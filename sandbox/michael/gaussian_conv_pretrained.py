
import lasagne
import lasagne.layers as L
import lasagne.nonlinearities as NL
from lasagne.layers import InputLayer, DenseLayer, DropoutLayer
# from ImageNet Pretrained Network (VGG_S)
from lasagne.layers.dnn import Conv2DDNNLayer as ConvLayer
from lasagne.layers import MaxPool2DLayer as PoolLayer
from lasagne.layers import LocalResponseNormalization2DLayer as NormLayer
import numpy as np
import pickle

from rllab.core.lasagne_layers import ParamLayer
from rllab.core.lasagne_powered import LasagnePowered
from rllab.core.network import ConvNetwork, MLP
from rllab.spaces import Box

from rllab.core.serializable import Serializable
from rllab.policies.base import StochasticPolicy
from rllab.misc.overrides import overrides
from rllab.misc import logger
from rllab.misc import ext
from rllab.distributions.diagonal_gaussian import DiagonalGaussian
import theano.tensor as TT



class GaussianConvPretrained(StochasticPolicy, LasagnePowered, Serializable):
    """
    A class for performing regression by fitting a Gaussian distribution to the outputs.
    """

    def __init__(
            self,
            env_spec,
            hidden_sizes=(128,128),
            hidden_nonlinearity=NL.tanh,
            std_hidden_nonlinearity=NL.tanh,
            output_nonlinearity=None,
            mean_network=None,

            optimizer=None,
            use_trust_region=True,
            step_size=0.01,
            subsample_factor=1.0,
            batchsize=None,

            learn_std=True,
            init_std=1.0,
            min_std=1e-6,
            adaptive_std=False,
            std_share_network=False,
            std_network=None,
            std_conv_filters=(),std_conv_filters_sizes=(),std_conv_strides=(),std_conv_pads=(),
            std_hidden_sizes=(32, 32),
            std_nonlinearity=None,
            dist_cls=DiagonalGaussian, # figure out what it does
            normalize_inputs=True,
            normalize_outputs=True,
    ):
        """
        :param env_spec:
        :param hidden_sizes: list of sizes for the fully-connected hidden layers
        :param learn_std: Is std trainable
        :param init_std: Initial std
        :param adaptive_std:
        :param std_share_network:
        :param std_hidden_sizes: list of sizes for the fully-connected layers for std
        :param min_std: whether to make sure that the std is at least some threshold value, to avoid numerical issues
        :param std_hidden_nonlinearity:
        :param hidden_nonlinearity: nonlinearity used for each hidden layer
        :param output_nonlinearity: nonlinearity for the output layer
        :param mean_network: custom network for the output mean
        :param std_network: custom network for the output log std
        :return:
        """
        Serializable.quick_init(self, locals())
        assert isinstance(env_spec.action_space, Box)

        obs_dim = env_spec.observation_space.shape
        action_dim = env_spec.action_space.flat_dim
        #TODO: verify correctness. Lasagne takes color channel in as first component
        obs_dim = (obs_dim[2], obs_dim[0], obs_dim[1])

        # create network
        if mean_network is None:
            net = {}
            net['input'] = InputLayer((None, 3, 224, 224))
            net['conv1'] = ConvLayer(net['input'], num_filters=96, filter_size=7, stride=2, flip_filters=False)
            net['norm1'] = NormLayer(net['conv1'], alpha=0.0001) # caffe has alpha = alpha * pool_size
            net['pool1'] = PoolLayer(net['norm1'], pool_size=3, stride=3, ignore_border=False)
            net['conv2'] = ConvLayer(net['pool1'], num_filters=256, filter_size=5, flip_filters=False)
            net['pool2'] = PoolLayer(net['conv2'], pool_size=2, stride=2, ignore_border=False)
            net['conv3'] = ConvLayer(net['pool2'], num_filters=512, filter_size=3, pad=1, flip_filters=False)
            net['conv4'] = ConvLayer(net['conv3'], num_filters=512, filter_size=3, pad=1, flip_filters=False)
            net['conv5'] = ConvLayer(net['conv4'], num_filters=512, filter_size=3, pad=1, flip_filters=False)
            net['pool5'] = PoolLayer(net['conv5'], pool_size=3, stride=3, ignore_border=False)
            model = pickle.load(open('vgg_cnn_s.pkl'))
            L.set_all_param_values(net['pool5'], model['values'][:10])
            l_hid = net['pool5']
            mean_network = MLP(
                input_shape=(net['pool5'].shape),
                output_dim=action_dim,
                hidden_sizes=hidden_sizes,
                hidden_nonlinearity=hidden_nonlinearity,
                output_nonlinearity=output_nonlinearity,
            )
        self._mean_network = mean_network

        l_mean = mean_network.output_layer
        obs_var = mean_network.input_layer.input_var

        if std_network is not None:
            l_log_std = std_network.output_layer
        else:
            if adaptive_std:
                std_network = ConvNetwork(
                    input_shape=obs_dim,
                    output_dim=action_dim,
                    hidden_sizes=std_hidden_sizes,
                    hidden_nonlinearity=std_hidden_nonlinearity,
                    output_nonlinearity=None,
                )
                l_log_std = std_network.output_layer
            else:
                l_log_std = ParamLayer(
                    mean_network.input_layer,
                    num_units=action_dim,
                    param=lasagne.init.Constant(np.log(init_std)),
                    name="output_log_std",
                    trainable=learn_std,
                )

        self.min_std = min_std

        mean_var, log_std_var = L.get_output([l_mean, l_log_std])

        if self.min_std is not None:
            log_std_var = TT.maximum(log_std_var, np.log(min_std))

        self._mean_var, self._log_std_var = mean_var, log_std_var

        self._l_mean = l_mean
        self._l_log_std = l_log_std

        self._dist = dist_cls(action_dim)

        LasagnePowered.__init__(self, [l_mean, l_log_std])
        super(GaussianConvPolicy, self).__init__(env_spec)

        self._f_dist = ext.compile_function(
            inputs=[obs_var],
            outputs=[mean_var, log_std_var],
        )

    def dist_info_sym(self, obs_var, state_info_vars=None):
        mean_var, log_std_var = L.get_output([self._l_mean, self._l_log_std], obs_var)
        if self.min_std is not None:
            log_std_var = TT.maximum(log_std_var, np.log(self.min_std))
        return dict(mean=mean_var, log_std=log_std_var)

    @overrides
    def get_action(self, observation):
        flat_obs = self.observation_space.flatten(observation)
        mean, log_std = [x[0] for x in self._f_dist([flat_obs])]
        rnd = np.random.normal(size=mean.shape)
        action = rnd * np.exp(log_std) + mean
        return action, dict(mean=mean, log_std=log_std)

    def get_actions(self, observations):
        flat_obs = self.observation_space.flatten_n(observations)
        means, log_stds = self._f_dist(flat_obs)
        rnd = np.random.normal(size=means.shape)
        actions = rnd * np.exp(log_stds) + means
        return actions, dict(mean=means, log_std=log_stds)

    def get_reparam_action_sym(self, obs_var, action_var, old_dist_info_vars):
        """
        Given observations, old actions, and distribution of old actions, return a symbolically reparameterized
        representation of the actions in terms of the policy parameters
        :param obs_var:
        :param action_var:
        :param old_dist_info_vars:
        :return:
        """
        new_dist_info_vars = self.dist_info_sym(obs_var, action_var)
        new_mean_var, new_log_std_var = new_dist_info_vars["mean"], new_dist_info_vars["log_std"]
        old_mean_var, old_log_std_var = old_dist_info_vars["mean"], old_dist_info_vars["log_std"]
        epsilon_var = (action_var - old_mean_var) / (TT.exp(old_log_std_var) + 1e-8)
        new_action_var = new_mean_var + epsilon_var * TT.exp(new_log_std_var)
        return new_action_var

    def log_diagnostics(self, paths):
        log_stds = np.vstack([path["agent_infos"]["log_std"] for path in paths])
        logger.record_tabular('AveragePolicyStd', np.mean(np.exp(log_stds)))

    @property
    def distribution(self):
        return self._dist
