import torch
import torch.nn as nn
from .lif import *


class RSynaptic(LIF):
    """
    2nd order recurrent leaky integrate and fire neuron model accounting for synaptic conductance.
    The synaptic current jumps upon spike arrival, which causes a jump in membrane potential.
    Synaptic current and membrane potential decay exponentially with rates of alpha and beta, respectively.
    For :math:`U[T] > U_{\\rm thr} ⇒ S[T+1] = 1`.

    If `reset_mechanism = "subtract"`, then :math:`U[t+1]` will have `threshold` subtracted from it whenever the neuron emits a spike:

    .. math::

            I_{\\rm syn}[t+1] = αI_{\\rm syn}[t] + VS_{\\rm out}[t] + I_{\\rm in}[t+1] \\\\
            U[t+1] = βU[t] + I_{\\rm syn}[t+1] - RU_{\\rm thr}

    If `reset_mechanism = "zero"`, then :math:`U[t+1]` will be set to `0` whenever the neuron emits a spike:

    .. math::

            I_{\\rm syn}[t+1] = αI_{\\rm syn}[t] + VS_{\\rm out}[t] + I_{\\rm in}[t+1] \\\\
            U[t+1] = βU[t] + I_{\\rm syn}[t+1] - R(βU[t] + I_{\\rm syn}[t+1])

    * :math:`I_{\\rm syn}` - Synaptic current
    * :math:`I_{\\rm in}` - Input current
    * :math:`U` - Membrane potential
    * :math:`U_{\\rm thr}` - Membrane threshold
    * :math:`S_{\\rm out}` - Output spike
    * :math:`R` - Reset mechanism: if active, :math:`R = 1`, otherwise :math:`R = 0`
    * :math:`α` - Synaptic current decay rate
    * :math:`β` - Membrane potential decay rate
    * :math:`V` - Explicit recurrent weight

    Example::

        import torch
        import torch.nn as nn
        import snntorch as snn

        alpha = 0.9
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
                self.lif1 = snn.RSynaptic(alpha=alpha, beta=beta, V=V1)
                self.fc2 = nn.Linear(num_hidden, num_outputs)
                self.lif2 = snn.RSynaptic(alpha=alpha, beta=beta, V=V2)

            def forward(self, x, syn1, mem1, spk1, syn2, mem2):
                cur1 = self.fc1(x)
                spk1, syn1, mem1 = self.lif1(cur1, spk1, syn1, mem1)
                cur2 = self.fc2(spk1)
                spk2, syn2, mem2 = self.lif2(cur2, spk2, syn2, mem2)
                return syn1, mem1, spk1, syn2, mem2, spk2


    For further reading, see:

    *R. B. Stein (1965) A theoretical analysis of neuron variability. Biophys. J. 5, pp. 173-194.*

    *R. B. Stein (1967) Some models of neuronal variability. Biophys. J. 7. pp. 37-68.*"""

    def __init__(
        self,
        alpha,
        beta,
        V,
        threshold=1.0,
        spike_grad=None,
        init_hidden=False,
        inhibition=False,
        learn_alpha=False,
        learn_beta=False,
        learn_threshold=False,
        learn_V=True,
        reset_mechanism="subtract",
        output=False,
    ):
        super(RSynaptic, self).__init__(
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

        self._alpha_register_buffer(alpha, learn_alpha)

        if self.init_hidden:
            self.spk, self.syn, self.mem = self.init_rsynaptic()
            self.state_fn = self.build_state_function_hidden
        else:
            self.state_fn = self.build_state_function

        self._V_register_buffer(V, learn_V)

    def forward(self, input_, spk=False, syn=False, mem=False):
        if (
            hasattr(spk, "init_flag")
            or hasattr(syn, "init_flag")
            or hasattr(mem, "init_flag")
        ):  # only triggered on first-pass
            spk, syn, mem = _SpikeTorchConv(spk, syn, mem, input_=input_)
        elif mem is False and hasattr(self.mem, "init_flag"):  # init_hidden case
            self.spk, self.syn, self.mem = _SpikeTorchConv(
                self.spk, self.syn, self.mem, input_=input_
            )

        if not self.init_hidden:
            self.reset = self.mem_reset(mem)
            syn, mem = self.state_fn(input_, spk, syn, mem)

            if self.inhibition:
                spk = self.fire_inhibition(mem.size(0), mem)
            else:
                spk = self.fire(mem)

            return spk, syn, mem

        # intended for truncated-BPTT where instance variables are hidden states
        if self.init_hidden:
            self._rsynaptic_forward_cases(spk, mem, syn)
            self.reset = self.mem_reset(self.mem)
            self.syn, self.mem = self.state_fn(input_)

            if self.inhibition:
                self.spk = self.fire_inhibition(self.mem.size(0), self.mem)
            else:
                self.spk = self.fire(self.mem)

            if self.output:
                return self.spk, self.syn, self.mem
            else:
                return self.spk

    def base_state_function(self, input_, spk, syn, mem):
        base_fn_syn = self.alpha.clamp(0, 1) * syn + input_ + self.V * spk
        base_fn_mem = self.beta.clamp(0, 1) * mem + base_fn_syn
        return base_fn_syn, base_fn_mem

    def base_state_reset_zero(self, input_, spk, syn, mem):
        base_fn_syn = self.alpha.clamp(0, 1) * syn + input_ + self.V * spk
        base_fn_mem = self.beta.clamp(0, 1) * mem + base_fn_syn
        return 0, base_fn_mem

    def build_state_function(self, input_, spk, syn, mem):
        if self.reset_mechanism_val == 0:  # reset by subtraction
            state_fn = tuple(
                map(
                    lambda x, y: x - y,
                    self.base_state_function(input_, spk, syn, mem),
                    (0, self.reset * self.threshold),
                )
            )
        elif self.reset_mechanism_val == 1:  # reset to zero
            state_fn = tuple(
                map(
                    lambda x, y: x - self.reset * y,
                    self.base_state_function(input_, spk, syn, mem),
                    self.base_state_reset_zero(input_, spk, syn, mem),
                )
            )
        elif self.reset_mechanism_val == 2:  # no reset, pure integration
            state_fn = self.base_state_function(input_, spk, syn, mem)
        return state_fn

    def base_state_function_hidden(self, input_):
        base_fn_syn = self.alpha.clamp(0, 1) * self.syn + input_ + self.V * self.spk
        base_fn_mem = self.beta.clamp(0, 1) * self.mem + base_fn_syn
        return base_fn_syn, base_fn_mem

    def base_state_reset_zero_hidden(self, input_):
        base_fn_syn = self.alpha.clamp(0, 1) * self.syn + input_ + self.V * self.spk
        base_fn_mem = self.beta.clamp(0, 1) * self.mem + base_fn_syn
        return 0, base_fn_mem

    def build_state_function_hidden(self, input_):
        if self.reset_mechanism_val == 0:  # reset by subtraction
            state_fn = tuple(
                map(
                    lambda x, y: x - y,
                    self.base_state_function_hidden(input_),
                    (0, self.reset * self.threshold),
                )
            )
        elif self.reset_mechanism_val == 1:  # reset to zero
            state_fn = tuple(
                map(
                    lambda x, y: x - self.reset * y,
                    self.base_state_function_hidden(input_),
                    self.base_state_function_hidden(input_),
                )
            )
        elif self.reset_mechanism_val == 2:  # no reset, pure integration
            state_fn = self.base_state_function_hidden(input_)
        return state_fn

    def _alpha_register_buffer(self, alpha, learn_alpha):
        if not isinstance(alpha, torch.Tensor):
            alpha = torch.as_tensor(alpha)
        if learn_alpha:
            self.alpha = nn.Parameter(alpha)
        else:
            self.register_buffer("alpha", alpha)

    def _rsynaptic_forward_cases(self, spk, mem, syn):
        if mem is not False or syn is not False or spk is not False:
            raise TypeError(
                "When `init_hidden=True`, RSynaptic expects 1 input argument."
            )

    @classmethod
    def detach_hidden(cls):
        """Returns the hidden states, detached from the current graph.
        Intended for use in truncated backpropagation through time where hidden state variables are instance variables."""

        for layer in range(len(cls.instances)):
            if isinstance(cls.instances[layer], RSynaptic):
                cls.instances[layer].syn.detach_()
                cls.instances[layer].mem.detach_()

    @classmethod
    def reset_hidden(cls):
        """Used to clear hidden state variables to zero.
        Intended for use where hidden state variables are instance variables."""

        for layer in range(len(cls.instances)):
            if isinstance(cls.instances[layer], RSynaptic):
                cls.instances[layer].syn = _SpikeTensor(init_flag=False)
                cls.instances[layer].mem = _SpikeTensor(init_flag=False)