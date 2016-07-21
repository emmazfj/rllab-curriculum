from __future__ import print_function
from __future__ import absolute_import

from sandbox.rocky.tf.algos.trpo import TRPO
from sandbox.rocky.hrl.envs.atari import AtariEnv
from sandbox.rocky.tf.envs.base import TfEnv
from sandbox.rocky.hrl_new.policies.fixed_clock_policy import FixedClockPolicy
from sandbox.rocky.straw.optimizers.tf_conjugate_gradient_optimizer import ConjugateGradientOptimizer, \
    FiniteDifferenceHvp
from rllab.misc.instrument import stub, run_experiment_lite
from rllab.baselines.linear_feature_baseline import LinearFeatureBaseline

stub(globals())
from rllab.misc.instrument import VariantGenerator

vg = VariantGenerator()
vg.add("seed", [x * 100 + 11 for x in range(5)])
vg.add("subgoal_dim", [10, 50])
vg.add("bottleneck_dim", [10, 50])
vg.add("subgoal_interval", [1, 3, 10])

variants = vg.variants()

print("#Experiments: %d" % len(variants))

for v in variants:
    env = TfEnv(AtariEnv(game="seaquest", obs_type="ram", frame_skip=4))
    policy = FixedClockPolicy(
        env_spec=env.spec,
        subgoal_dim=v["subgoal_dim"],
        bottleneck_dim=v["bottleneck_dim"],
        subgoal_interval=v["subgoal_interval"],
    )

    baseline = LinearFeatureBaseline(env_spec=env.spec)

    algo = TRPO(
        env=env,
        policy=policy,
        baseline=baseline,
        max_path_length=4500,
        batch_size=50000,
        discount=0.99,
        gae_lambda=0.99,
        optimizer=ConjugateGradientOptimizer(hvp_approach=FiniteDifferenceHvp(), accept_violation=False)
    )

    run_experiment_lite(
        algo.train(),
        exp_prefix="0708-hrl-atari-ram-1",
        seed=v["seed"],
        n_parallel=4,
        snapshot_mode="last",
        variant=v,
        mode="lab_kube",
    )