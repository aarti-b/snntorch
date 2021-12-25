import torch
import torch.nn as nn
from .lif import *


class RLeaky(LIF):
    """
    First-order recurrent leaky integrate-and-fire neuron model.
    Input is assumed to be a current injection appended to the voltage spike output.
    Membrane potential decays exponentially with rate beta.
    For :math:`U[T] > U_{\\rm thr} ⇒ S[T+1] = 1`.

    If `reset_mechanism = "subtract"`, then :math:`U[t+1]` will have `threshold` subtracted from it whenever the neuron emits a spike:

    .. math::

            U[t+1] = βU[t] + I_{\\rm in}[t+1] + VS_{\\rm out}[t] - RU_{\\rm thr}

    If `reset_mechanism = "zero"`, then :math:`U[t+1]` will be set to `0` whenever the neuron emits a spike:

    .. math::

            U[t+1] = βU[t] + I_{\\rm syn}[t+1] + VS_{\\rm out}[t] - R(βU[t] + I_{\\rm in}[t+1] + VS_{\\rm out}[t])

    * :math:`I_{\\rm in}` - Input current
    * :math:`U` - Membrane potential
    * :math:`U_{\\rm thr}` - Membrane threshold
    * :math:`S_{\\rm out}` - Output spike
    * :math:`R` - Reset mechanism: if active, :math:`R = 1`, otherwise :math:`R = 0`
    * :math:`β` - Membrane potential decay rate
    * :math:`V` - Explicit recurrent weight

    Example::

        import torch
        import torch.nn as nn
        import snntorch as snn

        beta = 0.5

        # shared recurrent connection for a given layer
        V1 = 0.5

        # independent connection p/neuron
        V2 = torch.rand(num_outputs)

        # Define Network
        class Net(nn.Module):
            def __init__(self):
                super().__init__()

                # initialize layers
                self.fc1 = nn.Linear(num_inputs, num_hidden)
                self.lif1 = snn.RLeaky(beta=beta, V=V1)
                self.fc2 = nn.Linear(num_hidden, num_outputs)
                self.lif2 = snn.RLeaky(beta=beta, V=V2)

            def forward(self, x, mem1, spk1, mem2):
                cur1 = self.fc1(x)
                spk1, mem1 = self.lif1(cur1, spk1, mem1)
                cur2 = self.fc2(spk1)
                spk2, mem2 = self.lif2(cur2, spk2, mem2)
                return mem1, spk1, mem2, spk2


    """

    def __init__(
        self,
        beta,
        V,
        threshold=1.0,
        spike_grad=None,
        init_hidden=False,
        inhibition=False,
        learn_beta=False,
        learn_threshold=False,
        learn_V=True,
        reset_mechanism="subtract",
        output=False,
    ):
        super(RLeaky, self).__init__(
            beta,
            threshold,
            spike_grad,
            init_hidden,
            inhibition,
            learn_beta,
            learn_threshold,
            reset_mechanism,
            output,
        )

        if self.init_hidden:
            self.spk, self.mem = self.init_rleaky()
            self.state_fn = self.build_state_function_hidden
        else:
            self.state_fn = self.build_state_function

        self._V_register_buffer(V, learn_V)

    def forward(self, input_, spk=False, mem=False):
        if hasattr(spk, "init_flag") or hasattr(
            mem, "init_flag"
        ):  # only triggered on first-pass
            spk, mem = _SpikeTorchConv(spk, mem, input_=input_)
        elif mem is False and hasattr(self.mem, "init_flag"):  # init_hidden case
            self.spk, self.mem = _SpikeTorchConv(self.spk, self.mem, input_=input_)

        # TO-DO: alternatively, we could do torch.exp(-1 / self.beta.clamp_min(0)),
        # giving actual time constants instead of values in [0, 1] as initial beta
        # beta = self.beta.clamp(0, 1)

        if not self.init_hidden:
            self.reset = self.mem_reset(mem)
            mem = self.state_fn(input_, spk, mem)

            if self.inhibition:
                spk = self.fire_inhibition(mem.size(0), mem)  # batch_size
            else:
                spk = self.fire(mem)

            return spk, mem

        # intended for truncated-BPTT where instance variables are hidden states
        if self.init_hidden:
            self._rleaky_forward_cases(spk, mem)
            self.reset = self.mem_reset(self.mem)
            self.mem = self.state_fn(input_)

            if self.inhibition:
                self.spk = self.fire_inhibition(self.mem.size(0), self.mem)
            else:
                self.spk = self.fire(self.mem)

            if self.output:  # read-out layer returns output+states
                return self.spk, self.mem
            else:  # hidden layer e.g., in nn.Sequential, only returns output
                return self.spk

    def base_state_function(self, input_, spk, mem):
        base_fn = self.beta.clamp(0, 1) * mem + input_ + self.V * spk
        return base_fn

    def build_state_function(self, input_, spk, mem):
        if self.reset_mechanism_val == 0:  # reset by subtraction
            state_fn = (
                self.base_state_function(input_, spk, mem) - self.reset * self.threshold
            )
        elif self.reset_mechanism_val == 1:  # reset to zero
            state_fn = self.base_state_function(
                input_, mem
            ) - self.reset * self.base_state_function(input_, spk, mem)
        elif self.reset_mechanism_val == 2:  # no reset, pure integration
            state_fn = self.base_state_function(input_, spk, mem)
        return state_fn

    def base_state_function_hidden(self, input_):
        base_fn = self.beta.clamp(0, 1) * self.mem + input_ + self.V * self.spk
        return base_fn

    def build_state_function_hidden(self, input_):
        if self.reset_mechanism_val == 0:  # reset by subtraction
            state_fn = (
                self.base_state_function_hidden(input_) - self.reset * self.threshold
            )
        elif self.reset_mechanism_val == 1:  # reset to zero
            state_fn = self.base_state_function_hidden(
                input_
            ) - self.reset * self.base_state_function_hidden(input_)
        elif self.reset_mechanism_val == 2:  # no reset, pure integration
            state_fn = self.base_state_function_hidden(input_)
        return state_fn

    def _rleaky_forward_cases(self, spk, mem):
        if mem is not False or spk is not False:
            raise TypeError("When `init_hidden=True`, RLeaky expects 1 input argument.")

    @classmethod
    def detach_hidden(cls):
        """Returns the hidden states, detached from the current graph.
        Intended for use in truncated backpropagation through time where hidden state variables are instance variables."""

        for layer in range(len(cls.instances)):
            if isinstance(cls.instances[layer], RLeaky):
                cls.instances[layer].mem.detach_()

    @classmethod
    def reset_hidden(cls):
        """Used to clear hidden state variables to zero.
        Intended for use where hidden state variables are instance variables.
        Assumes hidden states have a batch dimension already."""
        for layer in range(len(cls.instances)):
            if isinstance(cls.instances[layer], RLeaky):
                cls.instances[layer].mem = _SpikeTensor(init_flag=False)