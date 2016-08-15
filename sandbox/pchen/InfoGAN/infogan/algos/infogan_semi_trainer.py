from sandbox.pchen.InfoGAN.infogan.models.regularized_gan import RegularizedGAN
import prettytensor as pt
import tensorflow as tf
import numpy as np
from progressbar import ETA, Bar, Percentage, ProgressBar
from sandbox.pchen.InfoGAN.infogan.misc.distributions import Bernoulli, Gaussian, Categorical
import sys

TINY = 1e-8


class InfoGANSemiTrainer(object):
    def __init__(self,
                 model,
                 batch_size,
                 dataset=None,
                 scheduled_datasets=None,
                 exp_name="experiment",
                 log_dir="logs",
                 checkpoint_dir="ckt",
                 max_epoch=100,
                 updates_per_epoch=100,
                 snapshot_interval=10000,
                 info_reg_coeff=1.0,
                 cont_info_reg_coeff=None,
                 discriminator_learning_rate=2e-4,
                 generator_learning_rate=2e-4,
                 reg_epochs=0,
                 max_stuck_epochs=50,
                 semi_interval=1000,
                 semi_mode="modular",
                 semi_reg_coeff=None,
                 semi_cont_reg_coeff=None,
                 semi_feature="reg_latent_dist_flat",
                 semi_classifier="svm",
                 ):
        """
        :type model: RegularizedGAN
        :type semi_mode: basestring
        :param semi_mode: should be one of the following:
            "modular": first train InfoGAN, and then use the learned feature on supervised samples to train a classifier
            "min_entropy": while training InfoGAN, add extra semi-supervised terms, so that the recognition network
                also gets trained on real data (be certain about predictions on real data)
            "cross_entropy": similar to min_entropy, but use the true labels as supervised signals for the
                recognition network. This only works if we only have a single categorical latent code with the same
                dimension as the number of output categories
        :type semi_reg_coeff: float
        :param semi_reg_coeff: Coefficient for the entropy / cross entropy terms for discrete variables for
            semi-supervised learning. By default, this is set to info_reg_coeff
        :type semi_cont_reg_coeff: float
        :param semi_cont_reg_coeff: Coefficient for the entropy / cross entropy terms for continuous variables for
            semi-supervised learning. By default, this is set to cont_info_reg_coeff
        :type semi_feature: basestring
        :param semi_feature: should be one of the following:
            "reg_latent_dist_flat": use reg_dist_latent_flat as features and run SVM
            "reg_latent_dist_info": use the concatenated list of reg_latent_dist_info as features and run SVM
            "reg_latent_dist_both": use the concatenation of both reg_latent_dist_flat and reg_latent_dist_info
        """
        self.model = model
        if scheduled_datasets is None:
            if dataset is None:
                raise ValueError("Must specify either dataset or scheduled_datasets")
            scheduled_datasets = [(dataset, max_epoch)]
            self.dataset = dataset
        else:
            self.dataset = scheduled_datasets[0][0]
        self.scheduled_datasets = scheduled_datasets
        self.batch_size = batch_size
        self.max_epoch = max_epoch
        self.exp_name = exp_name
        self.log_dir = log_dir
        self.checkpoint_dir = checkpoint_dir
        self.snapshot_interval = snapshot_interval
        self.updates_per_epoch = updates_per_epoch
        self.generator_learning_rate = generator_learning_rate
        self.discriminator_learning_rate = discriminator_learning_rate
        self.info_reg_coeff = info_reg_coeff
        if cont_info_reg_coeff is None:
            cont_info_reg_coeff = info_reg_coeff
        self.cont_info_reg_coeff = cont_info_reg_coeff
        self.discriminator_trainer = None
        self.generator_trainer = None
        self.input_tensor = None
        self.reg_epochs = reg_epochs
        self.log_vars = []
        self.max_stuck_epochs = max_stuck_epochs
        self.semi_interval = semi_interval
        self.semi_mode = semi_mode
        if semi_reg_coeff is None:
            semi_reg_coeff = info_reg_coeff
        if semi_cont_reg_coeff is None:
            semi_cont_reg_coeff = cont_info_reg_coeff
        self.semi_reg_coeff = semi_reg_coeff
        self.semi_cont_reg_coeff = semi_cont_reg_coeff
        self.semi_feature = semi_feature
        self.semi_classifier = semi_classifier
        self.semi_syms = None

    def init_opt(self):
        self.input_tensor = input_tensor = tf.placeholder(tf.float32, [None, self.dataset.image_dim])
        self.sup_input_tensor = sup_input_tensor = tf.placeholder(tf.float32, [None, self.dataset.image_dim])
        self.sup_label_tensor = sup_label_tensor = tf.placeholder(tf.uint8, [None])

        self.real_cost_coeff = tf.placeholder(dtype=tf.float32, shape=[])

        with pt.defaults_scope(phase=pt.Phase.train):
            z_var = self.model.latent_dist.sample_prior(self.batch_size)
            fake_x, _ = self.model.generate(z_var)
            real_d, _, real_reg_z_dist_info, real_reg_z_dist_flat = self.model.discriminate(input_tensor)
            fake_d, _, fake_reg_z_dist_info, _ = self.model.discriminate(fake_x)

            reg_z = self.model.reg_z(z_var)

            discriminator_loss = - self.real_cost_coeff * tf.reduce_mean(
                tf.log(real_d + TINY) + tf.log(1. - fake_d + TINY))
            generator_loss = - self.real_cost_coeff * tf.reduce_mean(tf.log(fake_d + TINY))

            self.log_vars.append(("discriminator_loss", discriminator_loss))
            self.log_vars.append(("generator_loss", generator_loss))

            mi_est = tf.constant(0.)
            cross_ent = tf.constant(0.)

            # compute for discrete and continuous codes separately
            # discrete:
            if len(self.model.reg_disc_latent_dist.dists) > 0:
                disc_reg_z = self.model.disc_reg_z(reg_z)
                disc_reg_dist_info = self.model.disc_reg_dist_info(fake_reg_z_dist_info)
                disc_log_q_c_given_x = self.model.reg_disc_latent_dist.logli(disc_reg_z, disc_reg_dist_info)
                disc_log_q_c = self.model.reg_disc_latent_dist.logli_prior(disc_reg_z)
                disc_cross_ent = tf.reduce_mean(-disc_log_q_c_given_x)
                disc_ent = tf.reduce_mean(-disc_log_q_c)
                disc_mi_est = disc_ent - disc_cross_ent
                mi_est += disc_mi_est
                cross_ent += disc_cross_ent
                self.log_vars.append(("MI_disc", disc_mi_est))
                self.log_vars.append(("CrossEnt_disc", disc_cross_ent))
                discriminator_loss -= self.info_reg_coeff * disc_mi_est
                generator_loss -= self.info_reg_coeff * disc_mi_est

            if len(self.model.reg_cont_latent_dist.dists) > 0:
                cont_reg_z = self.model.cont_reg_z(reg_z)
                cont_reg_dist_info = self.model.cont_reg_dist_info(fake_reg_z_dist_info)
                cont_log_q_c_given_x = self.model.reg_cont_latent_dist.logli(cont_reg_z, cont_reg_dist_info)
                cont_log_q_c = self.model.reg_cont_latent_dist.logli_prior(cont_reg_z)
                cont_cross_ent = tf.reduce_mean(-cont_log_q_c_given_x)
                cont_ent = tf.reduce_mean(-cont_log_q_c)
                cont_mi_est = cont_ent - cont_cross_ent
                mi_est += cont_mi_est
                cross_ent += cont_cross_ent
                self.log_vars.append(("MI_cont", cont_mi_est))
                self.log_vars.append(("CrossEnt_cont", cont_cross_ent))
                discriminator_loss -= self.cont_info_reg_coeff * cont_mi_est
                generator_loss -= self.cont_info_reg_coeff * cont_mi_est

            if self.semi_mode == "modular":
                # no extra terms needed
                pass
            elif self.semi_mode in ["min_entropy", "cross_entropy"]:
                if len(self.model.reg_disc_latent_dist.dists) > 0:
                    real_disc_reg_dist_info = self.model.disc_reg_dist_info(real_reg_z_dist_info)
                    real_disc_ent = tf.reduce_mean(self.model.reg_disc_latent_dist.entropy(real_disc_reg_dist_info))
                    discriminator_loss += self.semi_reg_coeff * real_disc_ent

                if len(self.model.reg_cont_latent_dist.dists) > 0:
                    real_cont_reg_dist_info = self.model.cont_reg_dist_info(real_reg_z_dist_info)
                    real_cont_ent = tf.reduce_mean(self.model.reg_cont_latent_dist.entropy(real_cont_reg_dist_info))
                    discriminator_loss += self.semi_cont_reg_coeff * real_cont_ent
                if self.semi_mode == "cross_entropy":
                    # In addition, add supervised signal
                    # to be implemented
                    raise NotImplementedError
            else:
                raise NotImplementedError

            for idx, dist_info in enumerate(self.model.reg_latent_dist.split_dist_info(fake_reg_z_dist_info)):
                if "stddev" in dist_info:
                    self.log_vars.append(("max_std_%d" % idx, tf.reduce_max(dist_info["stddev"])))
                    self.log_vars.append(("min_std_%d" % idx, tf.reduce_min(dist_info["stddev"])))

            self.log_vars.append(("MI", mi_est))
            self.log_vars.append(("CrossEnt", cross_ent))

            all_vars = tf.trainable_variables()
            d_vars = [var for var in all_vars if var.name.startswith('d_')]
            g_vars = [var for var in all_vars if var.name.startswith('g_')]

            self.log_vars.append(("max_real_d", tf.reduce_max(real_d)))
            self.log_vars.append(("min_real_d", tf.reduce_min(real_d)))
            self.log_vars.append(("max_fake_d", tf.reduce_max(fake_d)))
            self.log_vars.append(("min_fake_d", tf.reduce_min(fake_d)))

            discriminator_optimizer = tf.train.AdamOptimizer(self.discriminator_learning_rate, beta1=0.5)
            self.discriminator_trainer = pt.apply_optimizer(discriminator_optimizer, losses=[discriminator_loss],
                                                            var_list=d_vars)

            generator_optimizer = tf.train.AdamOptimizer(self.generator_learning_rate, beta1=0.5)
            self.generator_trainer = pt.apply_optimizer(generator_optimizer, losses=[generator_loss], var_list=g_vars)

            for k, v in self.log_vars:
                tf.scalar_summary(k, v)

            self.semi_syms = dict(
                real_reg_z_dist_info=real_reg_z_dist_info,
                real_reg_z_dist_flat=real_reg_z_dist_flat,
            )

        with pt.defaults_scope(phase=pt.Phase.test):
            with tf.variable_scope("model", reuse=True) as scope:
                self.visualize_all_factors()

    def visualize_all_factors(self):
        with tf.Session():
            fixed_noncat = np.concatenate([
                np.tile(
                    self.model.nonreg_latent_dist.sample_prior(10).eval(),
                    [10, 1]
                ),
                self.model.nonreg_latent_dist.sample_prior(self.batch_size - 100).eval(),
            ], axis=0)
            fixed_cat = np.concatenate([
                np.tile(
                    self.model.reg_latent_dist.sample_prior(10).eval(),
                    [10, 1]
                ),
                self.model.reg_latent_dist.sample_prior(self.batch_size - 100).eval(),
            ], axis=0)

        offset = 0
        for dist_idx, dist in enumerate(self.model.reg_latent_dist.dists):
            if isinstance(dist, Gaussian):
                assert dist.dim == 1, "Only dim=1 is currently supported"
                c_vals = []
                for idx in xrange(10):
                    c_vals.extend([-1.0 + idx * 2.0 / 9] * 10)
                c_vals.extend([0.] * (self.batch_size - 100))
                vary_cat = np.asarray(c_vals, dtype=np.float32).reshape((-1, 1))
                cur_cat = np.copy(fixed_cat)
                cur_cat[:, offset:offset + 1] = vary_cat
                offset += 1
            elif isinstance(dist, Categorical):
                # assert dist.dim == 10, "Only dim=10 is currently supported"
                lookup = np.eye(dist.dim, dtype=np.float32)
                cat_ids = []
                for idx in xrange(10):
                    cat_ids.extend([idx] * 10)
                cat_ids.extend([0] * (self.batch_size - 100))
                cur_cat = np.copy(fixed_cat)
                cur_cat[:, offset:offset + dist.dim] = lookup[cat_ids]
                offset += dist.dim
            elif isinstance(dist, Bernoulli):
                assert dist.dim == 1, "Only dim=1 is currently supported"
                lookup = np.eye(dist.dim, dtype=np.float32)
                cat_ids = []
                for idx in xrange(10):
                    cat_ids.extend([int(idx / 5)] * 10)
                cat_ids.extend([0] * (self.batch_size - 100))
                cur_cat = np.copy(fixed_cat)
                cur_cat[:, offset:offset + dist.dim] = np.expand_dims(np.array(cat_ids), axis=-1)
                # import ipdb; ipdb.set_trace()
                offset += dist.dim
            else:
                raise NotImplementedError
            z_var = tf.constant(np.concatenate([fixed_noncat, cur_cat], axis=1))

            _, x_dist_info = self.model.generate(z_var)

            # just take the mean image
            if isinstance(self.model.output_dist, Bernoulli):
                img_var = x_dist_info["p"]
            elif isinstance(self.model.output_dist, Gaussian):
                img_var = x_dist_info["mean"]
            else:
                raise NotImplementedError
            img_var = self.dataset.inverse_transform(img_var)
            rows = 10
            img_var = tf.reshape(img_var, [self.batch_size] + list(self.dataset.image_shape))
            img_var = img_var[:rows * rows, :, :, :]
            imgs = tf.reshape(img_var, [rows, rows] + list(self.dataset.image_shape))
            stacked_img = []
            for row in xrange(rows):
                row_img = []
                for col in xrange(rows):
                    row_img.append(imgs[row, col, :, :, :])
                stacked_img.append(tf.concat(1, row_img))
            imgs = tf.concat(0, stacked_img)
            imgs = tf.expand_dims(imgs, 0)
            tf.image_summary("image_%d_%s" % (dist_idx, dist.__class__.__name__), imgs)

    def semi_eval(self, sess):
        from sklearn.svm import SVC
        from sklearn.neighbors import KNeighborsClassifier
        from sklearn.neural_network import MLPClassifier
        sup_images = self.dataset.supervised_train.images
        sup_labels = self.dataset.supervised_train.labels
        real_reg_z_dist_info = self.semi_syms["real_reg_z_dist_info"]
        real_reg_z_dist_flat = self.semi_syms["real_reg_z_dist_flat"]
        info_items = real_reg_z_dist_info.items()
        info_keys = [x[0] for x in info_items]
        info_vals = [x[1] for x in info_items]
        if self.semi_mode == "reg_latent_dist_flat":
            features = real_reg_z_dist_flat
        elif self.semi_mode == "reg_latent_dist_info":
            features = tf.concat(1, info_vals)
        elif self.semi_mode == "reg_latent_dist_both":
            features = tf.concat(1, [real_reg_z_dist_flat] + info_vals)
        else:
            raise NotImplementedError
        sup_features = sess.run(features, feed_dict={self.input_tensor: sup_images})
        val_images = self.dataset.validation.images
        val_labels = self.dataset.validation.labels
        val_features = sess.run(features, feed_dict={self.input_tensor: val_images})
        test_images = self.dataset.test.images
        test_labels = self.dataset.test.labels
        test_features = sess.run(features, feed_dict={self.input_tensor: test_images})
        if self.semi_classifier == "svm":
            clf = SVC()
        elif self.semi_classifier == "knn":
            clf = KNeighborsClassifier(n_neighbors=3)
        elif self.semi_classifier == "mlp":
            clf = MLPClassifier(algorithm='l-bfgs', hidden_layer_sizes=(100, 100))
        else:
            raise NotImplementedError
        clf.fit(sup_features, sup_labels)
        train_acc = np.mean(np.equal(clf.predict(sup_features), sup_labels))
        val_acc = np.mean(np.equal(clf.predict(val_features), val_labels))
        test_acc = np.mean(np.equal(clf.predict(test_features), test_labels))
        from tensorflow.core.framework import summary_pb2
        s = summary_pb2.Summary(
            value=[
                summary_pb2.Summary.Value(
                    tag="train_acc",
                    simple_value=train_acc,
                ),
                summary_pb2.Summary.Value(
                    tag="val_acc",
                    simple_value=val_acc,
                ),
                summary_pb2.Summary.Value(
                    tag="test_acc",
                    simple_value=test_acc,
                ),
            ]
        )
        print("Train Acc: %f; Val Acc: %f; Test Acc: %f" % (train_acc, val_acc, test_acc))
        return s

    def train(self):

        self.init_opt()

        init = tf.initialize_all_variables()

        n_stuck_epochs = 0

        with tf.Session() as sess:
            sess.run(init)

            summary_op = tf.merge_all_summaries()
            summary_writer = tf.train.SummaryWriter(self.log_dir, sess.graph)

            saver = tf.train.Saver()

            counter = 0

            log_vars = [x for _, x in self.log_vars]
            log_keys = [x for x, _ in self.log_vars]

            for dataset_idx, (dataset, max_epoch) in enumerate(self.scheduled_datasets):
                self.dataset = dataset

                for epoch in range(max_epoch):
                    if epoch < self.reg_epochs:
                        real_cost_coeff = 0.
                    else:
                        real_cost_coeff = 1.
                    widgets = ["epoch #%d|" % epoch, Percentage(), Bar(), ETA()]
                    pbar = ProgressBar(maxval=self.updates_per_epoch, widgets=widgets)
                    pbar.start()

                    all_log_vals = []
                    for i in range(self.updates_per_epoch):
                        pbar.update(i)
                        x, _ = self.dataset.train.next_batch(self.batch_size)
                        feed_dict = {self.input_tensor: x, self.real_cost_coeff: real_cost_coeff}
                        log_vals = sess.run([self.discriminator_trainer] + log_vars, feed_dict)[1:]
                        sess.run(self.generator_trainer, feed_dict)
                        all_log_vals.append(log_vals)
                        counter += 1

                        if counter % self.snapshot_interval == 0:
                            snapshot_name = "%s_%s" % (self.exp_name, str(counter))
                            fn = saver.save(sess, "%s/%s.ckpt" % (self.checkpoint_dir, snapshot_name))
                            print("Model saved in file: %s" % fn)

                    x, _ = self.dataset.train.next_batch(self.batch_size)

                    real_cost_coeff = 1.
                    summary_str = sess.run(summary_op, {self.input_tensor: x, self.real_cost_coeff: real_cost_coeff})
                    summary_writer.add_summary(summary_str, counter)
                    s = self.semi_eval(sess)
                    summary_writer.add_summary(s, counter)

                    avg_log_vals = np.mean(np.array(all_log_vals), axis=0)
                    log_dict = dict(zip(log_keys, avg_log_vals))

                    if epoch >= self.reg_epochs and \
                            (min(log_dict["discriminator_loss"], log_dict["generator_loss"]) < 1e-3 or log_dict[
                                "max_fake_d"] < 0.01):
                        n_stuck_epochs += 1
                    else:
                        n_stuck_epochs = 0
                    log_line = "; ".join("%s: %s" % (str(k), str(v)) for k, v in zip(log_keys, avg_log_vals))
                    print("Dataset %d | Epoch %d | " % (dataset_idx, epoch) + log_line)
                    print("Stuck for %d epochs" % n_stuck_epochs)
                    sys.stdout.flush()
                    if np.any(np.isnan(avg_log_vals)):
                        raise ValueError("NaN detected!")
                    if n_stuck_epochs >= self.max_stuck_epochs:
                        raise ValueError("Stuck for too long. Terminated!")