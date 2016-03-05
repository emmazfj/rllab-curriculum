import lasagne.layers as L
import lasagne.nonlinearities as NL
import numpy as np
from pydoc import locate
from rllab.core.lasagne_powered import LasagnePowered
from rllab.core.serializable import Serializable
from rllab.misc.overrides import overrides
from rllab.misc.special import weighted_sample, to_onehot
from rllab.misc.ext import compile_function
from rllab.misc import autoargs
from rllab.policy.base import StochasticPolicy
from rllab.misc import categorical_dist


class CategoricalMLPPolicy(StochasticPolicy, LasagnePowered, Serializable):

    @autoargs.arg('hidden_sizes', type=int, nargs='*',
                  help='list of sizes for the fully-connected hidden layers')
    @autoargs.arg('nonlinearity', type=str,
                  help='nonlinearity used for each hidden layer, can be one '
                       'of tanh, sigmoid')
    def __init__(
            self,
            mdp,
            hidden_sizes=(32, 32),
            nonlinearity='lasagne.nonlinearities.rectify'):
        Serializable.quick_init(self, locals())
        self._mdp = mdp
        self._nonlinearity = nonlinearity
        if isinstance(nonlinearity, str):
            nonlinearity = locate(nonlinearity)
        # create network
        l_obs = L.InputLayer(shape=(None,) + mdp.observation_shape)
        obs_var = l_obs.input_var

        l_hidden = l_obs
        for idx, hidden_size in enumerate(hidden_sizes):
            l_hidden = L.DenseLayer(
                l_hidden,
                num_units=hidden_size,
                nonlinearity=nonlinearity,
                name="h%d" % idx
            )
        l_prob = L.DenseLayer(
            l_hidden,
            num_units=mdp.action_dim,
            nonlinearity=NL.softmax,
            name="output_prob"
        )

        self._l_prob = l_prob
        self._l_obs = l_obs
        self._f_prob = compile_function([obs_var], L.get_output(l_prob))

        super(CategoricalMLPPolicy, self).__init__(mdp)
        LasagnePowered.__init__(self, [l_prob])

    @overrides
    def get_pdist_sym(self, obs_var):
        return L.get_output(self._l_prob, {self._l_obs: obs_var})

    @overrides
    def kl(self, old_prob_var, new_prob_var):
        return categorical_dist.kl_sym(old_prob_var, new_prob_var)

    @overrides
    def likelihood_ratio(self, old_prob_var, new_prob_var, action_var):
        return categorical_dist.likelihood_ratio_sym(
            action_var, old_prob_var, new_prob_var)

    @overrides
    def compute_entropy(self, pdist):
        return np.mean(categorical_dist.entropy(pdist))

    # The return value is a pair. The first item is a matrix (N, A), where each
    # entry corresponds to the action value taken. The second item is a vector
    # of length N, where each entry is the density value for that action, under
    # the current policy
    @overrides
    def get_action(self, observation):
        prob = self._f_prob([observation])[0]
        action = weighted_sample(prob, xrange(self.action_dim))
        return to_onehot(action, self.action_dim), prob