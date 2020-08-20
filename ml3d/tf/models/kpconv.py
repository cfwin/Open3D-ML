from os import makedirs
from os.path import exists
import time
import tensorflow as tf
import numpy as np
import random
from os.path import exists, join, isfile, dirname, abspath, split
import sys
from pathlib import Path
from sklearn.neighbors import KDTree

from ...datasets.utils.dataprocessing import DataProcessing

from .utils.kernels.kernel_points import load_kernels as create_kernel_points

# Convolution functions
# import network_blocks
# from .network_blocks import assemble_FCNN_blocks, segmentation_head, multi_segmentation_head
# from .network_blocks import segmentation_loss, multi_segmentation_loss

# Load custom operation
# BASE_DIR = Path(abspath(__file__))

# tf_neighbors_module = tf.load_op_library(
#     str(BASE_DIR.parent.parent / 'utils' / 'tf_custom_ops' /
#         'tf_neighbors.so'))
# tf_batch_neighbors_module = tf.load_op_library(
#     str(BASE_DIR.parent.parent / 'utils' / 'tf_custom_ops' /
#         'tf_batch_neighbors.so'))
# tf_subsampling_module = tf.load_op_library(
#     str(BASE_DIR.parent.parent / 'utils' / 'tf_custom_ops' /
#         'tf_subsampling.so'))
# tf_batch_subsampling_module = tf.load_op_library(
#     str(BASE_DIR.parent.parent / 'utils' / 'tf_custom_ops' /
#         'tf_batch_subsampling.so'))


def tf_batch_subsampling(points, batches_len, sampleDl):
    return tf_batch_subsampling_module.batch_grid_subsampling(
        points, batches_len, sampleDl)


def tf_batch_neighbors(queries, supports, q_batches, s_batches, radius):
    return tf_batch_neighbors_module.batch_ordered_neighbors(
        queries, supports, q_batches, s_batches, radius)

def get_weight(shape):
    # tf.set_random_seed(42)
    initial = tf.keras.initializers.TruncatedNormal(
        mean = 0.0, stddev = np.sqrt(2 / shape[-1])
    )
    weight = initial(shape = shape, dtype=tf.float32)
    weight = tf.round(weight * tf.constant(1000, dtype=tf.float32)) / tf.constant(1000, dtype=tf.float32)

    return tf.Variable(initial_value=weight, trainable=True, name='weight')

def get_bias(shape):
    initial = tf.zeros_initializer()
    return tf.Variable(initial_value=initial(shape=shape, dtype="float32"), trainable=True, name='bias')

def radius_gaussian(sq_r, sig, eps=1e-9):
    """
    Compute a radius gaussian (gaussian of distance)
    :param sq_r: input radiuses [dn, ..., d1, d0]
    :param sig: extents of gaussians [d1, d0] or [d0] or float
    :return: gaussian of sq_r [dn, ..., d1, d0]
    """
    return tf.exp(-sq_r / (2 * tf.square(sig) + eps))

def max_pool(x, inds):
    """
    Pools features with the maximum values.
    :param x: [n1, d] features matrix
    :param inds: [n2, max_num] pooling indices
    :return: [n2, d] pooled features matrix
    """

    # Add a last row with minimum features for shadow pools
    x = tf.concat([x, tf.reduce_min(x, axis=0, keep_dims=True)], axis=0)

    # Get all features for each pooling location [n2, max_num, d]
    pool_features = tf.gather(x, inds, axis=0)

    # Pool the maximum [n2, d]
    return tf.reduce_max(pool_features, axis=1)


def closest_pool(x, inds):
    """
    This tensorflow operation compute a pooling according to the list of indices 'inds'.
    > x = [n1, d] features matrix
    > inds = [n2, max_num] We only use the first column of this which should be the closest points too pooled positions
    >> output = [n2, d] pooled features matrix
    """

    # Add a last row with minimum features for shadow pools
    x = tf.concat([x, tf.zeros((1, int(x.shape[1])), x.dtype)], axis=0)

    # Get features for each pooling cell [n2, d]
    pool_features = tf.gather(x, inds[:, 0], axis=0)

    return pool_features

def global_average(x, batch_lengths):
    """
    Block performing a global average over batch pooling
    :param x: [N, D] input features
    :param batch_lengths: [B] list of batch lengths
    :return: [B, D] averaged features
    """

    # Loop over the clouds of the batch
    averaged_features = []
    i = 0
    for b_i, length in enumerate(batch_lengths):

        # Average features for each batch cloud
        averaged_features.append(tf.reduce_mean(x[i:i + length], axis=0))

        # Increment for next cloud
        i += length

    # Average features in each batch
    return tf.stack(averaged_features)

def block_decider(block_name,
                  radius,
                  in_dim,
                  out_dim,
                  layer_ind,
                  cfg):

    if block_name == 'unary':
        return UnaryBlock(in_dim, out_dim, cfg.use_batch_norm, cfg.batch_norm_momentum)

    elif block_name in ['simple',
                        'simple_deformable',
                        'simple_invariant',
                        'simple_equivariant',
                        'simple_strided',
                        'simple_deformable_strided',
                        'simple_invariant_strided',
                        'simple_equivariant_strided']:
        return SimpleBlock(block_name, in_dim, out_dim, radius, layer_ind, cfg)

    elif block_name in ['resnetb',
                        'resnetb_invariant',
                        'resnetb_equivariant',
                        'resnetb_deformable',
                        'resnetb_strided',
                        'resnetb_deformable_strided',
                        'resnetb_equivariant_strided',
                        'resnetb_invariant_strided']:
        return ResnetBottleneckBlock(block_name, in_dim, out_dim, radius, layer_ind, cfg)

    elif block_name == 'max_pool' or block_name == 'max_pool_wide':
        return MaxPoolBlock(layer_ind)

    elif block_name == 'global_average':
        return GlobalAverageBlock()

    elif block_name == 'nearest_upsample':
        return NearestUpsampleBlock(layer_ind)

    else:
        raise ValueError('Unknown block name in the architecture definition : ' + block_name)


class KPConv(tf.keras.layers.Layer):
  
    def __init__(self, kernel_size, p_dim, in_channels, out_channels, KP_extent, radius, 
                fixed_kernel_points='center', KP_influence='linear', aggregation_mode='sum',
                deformable=False, modulated=False, **kwargs):

        super(KPConv, self).__init__(**kwargs)

        self.KP_extent = KP_extent # TODO : verify correct kp extent
        self.K = kernel_size
        self.p_dim = p_dim
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.radius = radius
        self.fixed_kernel_points = fixed_kernel_points
        self.KP_influence = KP_influence
        self.aggregation_mode = aggregation_mode
        self.deformable = deformable
        self.modulated = modulated

        self.min_d2 = None
        self.deformed_KP = None
        self.offset_features = None

        self.wts = get_weight((self.K, self.in_channels, self.out_channels))

        if deformable:
            if modulated:
                self.offset_dim = (self.p_dim + 1) * self.K
            else:
                self.offset_dim = self.p_dim * self.K
            self.offset_conv = KPConv(self.K,
                                      self.p_dim,
                                      self.in_channels,
                                      self.offset_dim,
                                      KP_extent,
                                      radius,
                                      fixed_kernel_points=fixed_kernel_points,
                                      KP_influence=KP_influence,
                                      aggregation_mode=aggregation_mode)
            self.offset_bias = get_bias(self.offset_dim)

        else:
            self.offset_dim = None
            self.offset_conv = None
            self.offset_bias = None

        self.reset_parameters()

        self.kernel_points = self.init_KP()
        return

    def reset_parameters(self):
        init = tf.keras.initializers.HeUniform() # TODO : kaining initializer
        self.wts = tf.Variable(init(shape = self.wts.shape))

        if self.deformable:
            self.offset_bias = get_bias(self.offset_bias.shape)
        return

    def init_KP(self):
        K_points_numpy = create_kernel_points(self.radius,
                                      self.K,
                                      dimension=self.p_dim,
                                      fixed=self.fixed_kernel_points)

        # TODO : reshape to (num_kernel_points, points_dim)
        return tf.Variable(K_points_numpy.astype(np.float32), trainable=False, name='kernel_points')

    def call(self, query_points, support_points, neighbors_indices, features):
        # Get variables
        n_kp = int(self.kernel_points.shape[0])

        if self.deformable:
            # Get offsets with a KPConv that only takes part of the features
            self.offset_features = self.offset_conv(query_points, support_points, neighbors_indices, features) + self.offset_bias

            if self.modulated:
                # Get offset (in normalized scale) from features
                unscaled_offsets = self.offset_features[:, :self.p_dim * self.K]
                unscaled_offsets = tf.reshape(unscaled_offsets, (-1, self.K, self.p_dim))

                # Get modulations
                modulations = 2 * tf.sigmoid(self.offset_features[:, self.p_dim * self.K:])

            else:
                # Get offset (in normalized scale) from features
                unscaled_offsets = tf.reshape(self.offset_features, (-1, self.K, self.p_dim))

                # No modulations
                modulations = None

            # Rescale offset for this layer
            offsets = unscaled_offsets * self.KP_extent

        else:
            offsets = None
            modulations = None

        # Add a fake point in the last row for shadow neighbors
        shadow_point = tf.ones_like(support_points[:1, :]) * 1e6
        support_points = tf.concat([support_points, shadow_point], axis=0)

        # Get neighbor points [n_points, n_neighbors, dim]
        neighbors = tf.gather(support_points, neighbors_indices, axis=0)

        # Center every neighborhood
        neighbors = neighbors - tf.expand_dims(query_points, 1)

        if(self.deformable):
            self.deformed_KP = offsets + self.kernel_points
            deformed_K_points = tf.expand_dims(self.deformed_KP, 1)
        else:
            deformed_K_points = self.kernel_points

        # Get all difference matrices [n_points, n_neighbors, n_kpoints, dim]
        neighbors = tf.expand_dims(neighbors, 2)
        neighbors = tf.tile(neighbors, [1, 1, n_kp, 1]) # TODO : not in pytorch ?
        differences = neighbors - deformed_K_points

        # Get the square distances [n_points, n_neighbors, n_kpoints]
        sq_distances = tf.reduce_sum(tf.square(differences), axis=3)


        # Optimization by ignoring points outside a deformed KP range
        if self.deformable:

            # Save distances for loss
            self.min_d2, _ = torch.min(sq_distances, dim=1)

            # Boolean of the neighbors in range of a kernel point [n_points, n_neighbors]
            in_range = tf.cast(tf.reduce_any(tf.less(sq_distances, self.KP_extent**2), axis=2), tf.int32)

            # New value of max neighbors
            new_max_neighb = tf.reduce_max(tf.reduce_sum(in_range, axis=1))

            # For each row of neighbors, indices of the ones that are in range [n_points, new_max_neighb]
            new_neighb_bool, new_neighb_inds = tf.math.top_k(in_range, k=new_max_neighb)

            # Gather new neighbor indices [n_points, new_max_neighb]
            new_neighbors_indices = tf.batch_gather(neighbors_indices, new_neighb_inds)

            # Gather new distances to KP [n_points, new_max_neighb, n_kpoints]
            new_sq_distances = tf.batch_gather(sq_distances, new_neighb_inds)

            # New shadow neighbors have to point to the last shadow point
            new_neighbors_indices *= new_neighb_bool
            new_neighbors_indices += (1 - new_neighb_bool) * int(support_points.shape[0])

        else:
            new_neighbors_indices = neighbors_indices

        # Get Kernel point influences [n_points, n_kpoints, n_neighbors]
        if KP_influence == 'constant':
            # Every point get an influence of 1.
            all_weights = tf.ones_like(sq_distances)
            all_weights = tf.transpose(all_weights, [0, 2, 1])

        elif KP_influence == 'linear':
            # Influence decrease linearly with the distance, and get to zero when d = KP_extent.
            all_weights = tf.maximum(1 - tf.sqrt(sq_distances) / KP_extent, 0.0)
            all_weights = tf.transpose(all_weights, [0, 2, 1])

        elif KP_influence == 'gaussian':
            # Influence in gaussian of the distance.
            sigma = KP_extent * 0.3
            all_weights = radius_gaussian(sq_distances, sigma)
            all_weights = tf.transpose(all_weights, [0, 2, 1])
        else:
            raise ValueError('Unknown influence function type (cfg.KP_influence)')

        # In case of closest mode, only the closest KP can influence each point
        if aggregation_mode == 'closest':
            neighbors_1nn = tf.argmin(sq_distances, axis=2, output_type=tf.int32)
            all_weights *= tf.one_hot(neighbors_1nn, self.K, axis=1, dtype=tf.float32) # TODO : transpose in pytorch not here ?

        elif aggregation_mode != 'sum':
            raise ValueError("Unknown convolution mode. Should be 'closest' or 'sum'")

        features = tf.concat([features, tf.zeros_like(features[:1, :])], axis=0)

        # Get the features of each neighborhood [n_points, n_neighbors, in_fdim]
        neighborhood_features = tf.gather(features, new_neighbors_indices, axis=0)

        # Apply distance weights [n_points, n_kpoints, in_fdim]
        weighted_features = tf.matmul(all_weights, neighborhood_features)

       # Apply modulations
        if self.deformable and self.modulated:
            weighted_features *= tf.expand_dims(modulations, 2)

        # Apply network weights [n_kpoints, n_points, out_fdim]
        weighted_features = tf.transpose(weighted_features, [1, 0, 2])
        kernel_outputs = tf.matmul(weighted_features, self.wts)

        # Convolution sum to get [n_points, out_fdim]
        output_features = tf.reduce_sum(kernel_outputs, axis=0)

        return output_features

    def __repr__(self):
        return 'KPConv(radius: {:.2f}, in_feat: {:d}, out_feat: {:d})'.format(self.radius,
                                                                              self.in_channels,
                                                                              self.out_channels)

class BatchNormBlock(tf.keras.layers.Layer):

    def __init__(self, in_dim, use_bn, bn_momentum):
        super(BatchNormBlock, self).__init__()
        self.bn_momentum = bn_momentum
        self.use_bn = use_bn
        self.in_dim = in_dim

        if(self.use_bn):
            self.batch_norm = tf.keras.layers.BatchNormalization(momentum=bn_momentum)
        else:
            self.bias = get_bias(shape=in_dim)

    def call(self, x):
        if(self.use_bn):
            return self.batch_norm(x)
        else:
            return x + self.bias

    def __repr__(self):
        return 'BatchNormBlock(in_feat: {:d}, momentum: {:.3f}, only_bias: {:s})'.format(self.in_dim,
                                                                                         self.bn_momentum,
                                                                                         str(not self.use_bn))

class UnaryBlock(tf.keras.layers.Layer):

    def __init__(self, in_dim, out_dim, use_bn, bn_momentum, no_relu=False):

        super(UnaryBlock, self).__init__()
        self.bn_momentum = bn_momentum
        self.use_bn = use_bn
        self.no_relu = no_relu
        self.in_dim = in_dim
        self.out_dim = out_dim
        # self.mlp = tf.keras.models.Sequential(
        #     tf.keras.Input(shape=(in_dim,),
        #     tf.keras.layers.Dense(out_dim, use_bias=False)
        # )
        self.mlp = tf.keras.layers.Dense(out_dim, use_bias=False)
        self.batch_norm = BatchNormBlock(out_dim, self.use_bn, self.bn_momentum)

        if not no_relu:
            self.leaky_relu = tf.keras.layers.LeakyReLU(0.1)

    def call(self, x):
        x = self.mlp(x) # TODO : check correct dimension is getting modified
        x = self.batch_norm(x)
        if not self.no_relu:
            x = self.leaky_relu(x)
        return x

    def __repr__(self):
        return 'UnaryBlock(in_feat: {:d}, out_feat: {:d}, BN: {:s}, ReLU: {:s})'.format(self.in_dim,
                                                                                        self.out_dim,
                                                                                        str(self.use_bn),
                                                                                        str(not self.no_relu))

class SimpleBlock(tf.keras.layers.Layer):

    def __init__(self, block_name, in_dim, out_dim, radius, layer_ind, cfg):
        super(SimpleBlock, self).__init__()

        current_extent = radius * cfg.KP_extent / cfg.conv_radius

        self.bn_momentum = cfg.batch_norm_momentum
        self.use_bn = cfg.use_batch_norm
        self.layer_ind = layer_ind
        self.block_name = block_name
        self.in_dim = in_dim
        self.out_dim = out_dim

        self.KPConv = KPConv(
            cfg.num_kernel_points,
            cfg.in_points_dim,
            in_dim,
            out_dim // 2,
            current_extent,
            radius,
            fixed_kernel_points=cfg.fixed_kernel_points,
            aggregation_mode=cfg.aggregation_mode,
            modulated=cfg.modulated
        )

        self.batch_norm = BatchNormBlock(out_dim // 2, self.use_bn, self.bn_momentum)
        self.leaky_relu = tf.keras.layers.LeakyReLU(0.1)

    def call(self, x, batch):

        # TODO : check x, batch
        if 'strided' in self.block_name:
            q_pts = batch.points[self.layer_ind + 1] # TODO : 1 will not come here.
            s_pts = batch.points[self.layer_ind]
            neighb_inds = batch.pools[self.layer_ind]
        else:
            q_pts = batch.points[self.layer_ind]
            s_pts = batch.points[self.layer_ind]
            neighb_inds = batch.neighbors[self.layer_ind]

        x = self.KPConv(q_pts, s_pts, neighb_inds, x)
        return self.leaky_relu(self.batch_norm(x))

class IdentityBlock(tf.keras.layers.Layer):

    def __init__(self):
        super(IdentityBlock, self).__init__()

    def call(self, x):
        return tf.identity(x)

class ResnetBottleneckBlock(tf.keras.layers.Layer):

    def __init__(self, block_name, in_dim, out_dim, radius, layer_ind, cfg):

        super(ResnetBottleneckBlock, self).__init__()

        # get KP_extent from current radius
        current_extent = radius * cfg.KP_extent / cfg.conv_radius

        # Get other parameters
        self.bn_momentum = cfg.batch_norm_momentum
        self.use_bn = cfg.use_batch_norm
        self.block_name = block_name
        self.layer_ind = layer_ind
        self.in_dim = in_dim
        self.out_dim = out_dim

        # First downscaling mlp
        if in_dim != out_dim // 4:
            self.unary1 = UnaryBlock(in_dim, out_dim // 4, self.use_bn, self.bn_momentum)
        else:
            self.unary1 = tf.identity()

        # KPConv block
        self.KPConv = KPConv(cfg.num_kernel_points,
                             cfg.in_points_dim,
                             out_dim // 4,
                             out_dim // 4,
                             current_extent,
                             radius,
                             fixed_kernel_points=cfg.fixed_kernel_points,
                             KP_influence=cfg.KP_influence,
                             aggregation_mode=cfg.aggregation_mode,
                             deformable='deform' in block_name,
                             modulated=cfg.modulated)

        self.batch_norm_conv = BatchNormBlock(out_dim // 4, self.use_bn, self.bn_momentum)

        # Second upscaling mlp
        self.unary2 = UnaryBlock(out_dim // 4, out_dim, self.use_bn, self.bn_momentum, no_relu=True)

        # Shortcut optional mpl
        if in_dim != out_dim:
            self.unary_shortcut = UnaryBlock(in_dim, out_dim, self.use_bn, self.bn_momentum, no_relu=True)
        else:
            self.unary_shortcut = IdentityBlock()

        # Other operations
        self.leaky_relu = tf.keras.layers.LeakyReLU(0.1)

        return

    def call(self, features, batch):

        if 'strided' in self.block_name:
            q_pts = batch.points[self.layer_ind + 1]
            s_pts = batch.points[self.layer_ind]
            neighb_inds = batch.pools[self.layer_ind]
        else:
            q_pts = batch.points[self.layer_ind]
            s_pts = batch.points[self.layer_ind]
            neighb_inds = batch.neighbors[self.layer_ind]

        # First downscaling mlp
        x = self.unary1(features)

        # Convolution
        x = self.KPConv(q_pts, s_pts, neighb_inds, x)
        x = self.leaky_relu(self.batch_norm_conv(x))

        # Second upscaling mlp
        x = self.unary2(x)

        # Shortcut
        if 'strided' in self.block_name:
            shortcut = max_pool(features, neighb_inds) # TODO : test max_pool
        else:
            shortcut = features
        shortcut = self.unary_shortcut(shortcut)

        return self.leaky_relu(x + shortcut)

class NearestUpsampleBlock(tf.keras.layers.Layer):

    def __init__(self, layer_ind):

        super(NearestUpsampleBlock, self).__init__()
        self.layer_ind = layer_ind
        return

    def call(self, x, batch):
        return closest_pool(x, batch.upsamples[self.layer_ind - 1])

    def __repr__(self):
        return 'NearestUpsampleBlock(layer: {:d} -> {:d})'.format(self.layer_ind,
                                                                  self.layer_ind - 1)

class MaxPoolBlock(tf.keras.layers.Layer):

    def __init__(self, layer_ind):

        super(MaxPoolBlock, self).__init__()
        self.layer_ind = layer_ind
        return

    def forward(self, x, batch):
        return max_pool(x, batch['pools'][self.layer_ind + 1]) # TODO : check 1 here


class GlobalAverageBlock(tf.keras.layers.Layer):

    def __init__(self):

        super(GlobalAverageBlock, self).__init__()
        return

    def forward(self, x, batch):
        return global_average(x, batch.lengths[-1])


class KPFCNN(tf.keras.Model):

    def __init__(self, cfg):
        super(KPFCNN, self).__init__()

        # Model parameters
        self.cfg = cfg

        # From config parameter, compute higher bound of neighbors number in a neighborhood
        hist_n = int(np.ceil(4 / 3 * np.pi * (cfg.density_parameter + 1) ** 3))

        # Initiate neighbors limit with higher bound
        self.neighborhood_limits = np.full(cfg.num_layers, hist_n, dtype=np.int32)

        # self.dropout_prob = tf.placeholder(tf.float32, name='dropout_prob')
        self.dropout_prob = tf.constant(0.2, name='dropout_prob')

        lbl_values = cfg.num_classes
        ign_lbls = cfg.ignored_label_inds

      # Current radius of convolution and feature dimension
        layer = 0
        r = cfg.first_subsampling_dl * cfg.conv_radius
        in_dim = cfg.in_features_dim
        out_dim = cfg.first_features_dim
        self.K = cfg.num_kernel_points
        self.C = len(lbl_values) - len(ign_lbls)

        # Save all block operations in a list of modules
        self.encoder_ops = tf.Keras.Sequential()
        self.decoder_ops = tf.Keras.Sequential()        
        self.block_ops = tf.keras.Sequential()

        # Loop over consecutive blocks
        block_in_layer = 0

        arch = []

        for block_i, block in enumerate(cfg.architecture):

            # Check equivariance
            if ('equivariant' in block) and (not out_dim % 3 == 0):
                raise ValueError('Equivariant block but features dimension is not a factor of 3')

            # Detect upsampling block to stop
            if 'upsample' in block:
                break

            # Apply the good block function defining tf ops
            arch.append(repr(block_decider(block,
                                                r,
                                                in_dim,
                                                out_dim,
                                                layer,
                                                cfg)))
            self.block_ops.add(block_decider(block,
                                                r,
                                                in_dim,
                                                out_dim,
                                                layer,
                                                cfg))


            # Index of block in this layer
            block_in_layer += 1

            # Update dimension of input from output
            if 'simple' in block:
                in_dim = out_dim // 2
            else:
                in_dim = out_dim


            # Detect change to a subsampled layer
            if 'pool' in block or 'strided' in block:
                # Update radius and feature dimension for next layer
                layer += 1
                r *= 2
                out_dim *= 2
                block_in_layer = 0


    def call(self, flat_inputs):
        cfg = self.cfg

        inputs = dict()
        inputs['points'] = flat_inputs[:cfg.num_layers]
        inputs['neighbors'] = flat_inputs[cfg.num_layers:2 * cfg.num_layers]
        inputs['pools'] = flat_inputs[2 * cfg.num_layers:3 * cfg.num_layers]
        inputs['upsamples'] = flat_inputs[3 * cfg.num_layers:4 * cfg.num_layers]

        ind = 4 * cfg.num_layers
        inputs['features'] = flat_inputs[ind]
        ind += 1
        inputs['batch_weights'] = flat_inputs[ind]
        ind += 1
        inputs['in_batches'] = flat_inputs[ind]
        ind += 1
        inputs['out_batches'] = flat_inputs[ind]
        ind += 1
        inputs['point_labels'] = flat_inputs[ind]
        ind += 1
        labels = inputs['point_labels']

        inputs['augment_scales'] = flat_inputs[ind]
        ind += 1
        inputs['augment_rotations'] = flat_inputs[ind]

        ind += 1
        inputs['point_inds'] = flat_inputs[ind]
        ind += 1
        inputs['cloud_inds'] = flat_inputs[ind]

        output_features = assemble_FCNN_blocks(inputs, cfg,
                                                self.dropout_prob)

        self.logits = segmentation_head(output_features, self.cfg,
                                        self.dropout_prob)


        # print(inputs)

    def big_neighborhood_filter(self, neighbors, layer):
        """
        Filter neighborhoods with max number of neighbors. Limit is set to keep XX% of the neighborhoods untouched.
        Limit is computed at initialization
        """
        # crop neighbors matrix
        return neighbors[:, :self.neighborhood_limits[layer]]

    def init_input(flat_inputs):
        # # Path of the result folder
        # if self.cfg.saving:
        #     if self.cfg.saving_path == None:
        #         self.saving_path = time.strftime(
        #             'results/Log_%Y-%m-%d_%H-%M-%S', time.gmtime())
        #     else:
        #         self.saving_path = self.cfg.saving_path
        #     if not exists(self.saving_path):
        #         makedirs(self.saving_path)

        # ########
        # # Inputs
        # ########

        # # Sort flatten inputs in a dictionary
        # with tf.variable_scope('inputs'):
        #     self.inputs = dict()
        #     self.inputs['points'] = flat_inputs[:cfg.num_layers]
        #     self.inputs['neighbors'] = flat_inputs[cfg.num_layers:2 *
        #                                            cfg.num_layers]
        #     self.inputs['pools'] = flat_inputs[2 * cfg.num_layers:3 *
        #                                        cfg.num_layers]
        #     self.inputs['upsamples'] = flat_inputs[3 * cfg.num_layers:4 *
        #                                            cfg.num_layers]
        #     ind = 4 * cfg.num_layers
        #     self.inputs['features'] = flat_inputs[ind]
        #     ind += 1
        #     self.inputs['batch_weights'] = flat_inputs[ind]
        #     ind += 1
        #     self.inputs['in_batches'] = flat_inputs[ind]
        #     ind += 1
        #     self.inputs['out_batches'] = flat_inputs[ind]
        #     ind += 1
        #     self.inputs['point_labels'] = flat_inputs[ind]
        #     ind += 1
        #     self.labels = self.inputs['point_labels']

        #     if cfg.network_model in [
        #             'multi_segmentation', 'multi_cloud_segmentation'
        #     ]:
        #         self.inputs['super_labels'] = flat_inputs[ind]
        #         ind += 1

        #     self.inputs['augment_scales'] = flat_inputs[ind]
        #     ind += 1
        #     self.inputs['augment_rotations'] = flat_inputs[ind]

        #     if cfg.network_model in [
        #             "cloud_segmentation", 'multi_cloud_segmentation'
        #     ]:
        #         ind += 1
        #         self.inputs['point_inds'] = flat_inputs[ind]
        #         ind += 1
        #         self.inputs['cloud_inds'] = flat_inputs[ind]

        #     elif cfg.network_model in [
        #             'multi_segmentation', 'segmentation'
        #     ]:
        #         ind += 1
        #         self.inputs['object_inds'] = flat_inputs[ind]

            # Dropout placeholder
            # self.dropout_prob = tf.placeholder(tf.float32, name='dropout_prob')

        ########
        # Layers
        ########

        # Create layers
        with tf.variable_scope('KernelPointNetwork'):
            output_features = assemble_FCNN_blocks(self.inputs, self.cfg,
                                                   self.dropout_prob)

            self.logits = segmentation_head(output_features, self.cfg,
                                            self.dropout_prob)

        ########
        # Losses
        ########

        with tf.variable_scope('loss'):

            if cfg.network_model in [
                    "multi_segmentation", 'multi_cloud_segmentation'
            ]:
                self.output_loss = multi_segmentation_loss(
                    self.logits,
                    self.inputs,
                    batch_average=self.cfg.batch_averaged_loss)

            elif len(self.cfg.ignored_label_inds) > 0:

                # Boolean mask of points that should be ignored
                ignored_bool = tf.zeros_like(self.labels, dtype=tf.bool)
                for ign_label in self.cfg.ignored_label_inds:
                    ignored_bool = tf.logical_or(
                        ignored_bool, tf.equal(self.labels, ign_label))

                # Collect logits and labels that are not ignored
                inds = tf.squeeze(tf.where(tf.logical_not(ignored_bool)))
                new_logits = tf.gather(self.logits, inds, axis=0)
                new_dict = {
                    'point_labels': tf.gather(self.labels, inds, axis=0)
                }

                # Reduce label values in the range of logit shape
                reducing_list = tf.range(self.cfg.num_classes,
                                         dtype=tf.int32)
                inserted_value = tf.zeros((1, ), dtype=tf.int32)
                for ign_label in self.cfg.ignored_label_inds:
                    reducing_list = tf.concat([
                        reducing_list[:ign_label], inserted_value,
                        reducing_list[ign_label:]
                    ], 0)
                new_dict['point_labels'] = tf.gather(reducing_list,
                                                     new_dict['point_labels'])

                # Add batch weigths to dict if needed
                if self.cfg.batch_averaged_loss:
                    new_dict['batch_weights'] = self.inputs['batch_weights']

                # Output loss
                self.output_loss = segmentation_loss(
                    new_logits,
                    new_dict,
                    batch_average=self.cfg.batch_averaged_loss)

            else:
                self.output_loss = segmentation_loss(
                    self.logits,
                    self.inputs,
                    batch_average=self.cfg.batch_averaged_loss)

            # Add regularization
            self.loss = self.regularization_losses() + self.output_loss

        return

    def regularization_losses(self):

        #####################
        # Regularization loss
        #####################

        # Get L2 norm of all weights
        regularization_losses = [
            tf.nn.l2_loss(v) for v in tf.global_variables()
            if 'weights' in v.name
        ]
        self.regularization_loss = self.cfg.weights_decay * tf.add_n(
            regularization_losses)

        ##############################
        # Gaussian regularization loss
        ##############################

        gaussian_losses = []
        for v in tf.global_variables():
            if 'kernel_extents' in v.name:

                # Layer index
                layer = int(v.name.split('/')[1].split('_')[-1])

                # Radius of convolution for this layer
                conv_radius = cfg.first_subsampling_dl * self.cfg.density_parameter * (
                    2**(layer - 1))

                # Target extent
                target_extent = conv_radius / 1.5
                gaussian_losses += [tf.nn.l2_loss(v - target_extent)]

        if len(gaussian_losses) > 0:
            self.gaussian_loss = self.cfg.gaussian_decay * tf.add_n(
                gaussian_losses)
        else:
            self.gaussian_loss = tf.constant(0, dtype=tf.float32)

        #############################
        # Offsets regularization loss
        #############################

        offset_losses = []

        if self.cfg.offsets_loss == 'permissive':

            for op in tf.get_default_graph().get_operations():
                if op.name.endswith('deformed_KP'):

                    # Get deformed positions
                    deformed_positions = op.outputs[0]

                    # Layer index
                    layer = int(op.name.split('/')[1].split('_')[-1])

                    # Radius of deformed convolution for this layer
                    conv_radius = cfg.first_subsampling_dl * self.cfg.density_parameter * (
                        2**layer)

                    # Normalized KP locations
                    KP_locs = deformed_positions / conv_radius

                    # Loss will be zeros inside radius and linear outside radius
                    # Mean => loss independent from the number of input points
                    radius_outside = tf.maximum(0.0,
                                                tf.norm(KP_locs, axis=2) - 1.0)
                    offset_losses += [tf.reduce_mean(radius_outside)]

        elif self.cfg.offsets_loss == 'fitting':

            for op in tf.get_default_graph().get_operations():

                if op.name.endswith('deformed_d2'):

                    # Get deformed distances
                    deformed_d2 = op.outputs[0]

                    # Layer index
                    layer = int(op.name.split('/')[1].split('_')[-1])

                    # Radius of deformed convolution for this layer
                    KP_extent = cfg.first_subsampling_dl * cfg.KP_extent * (
                        2**layer)

                    # Get the distance to closest input point
                    KP_min_d2 = tf.reduce_min(deformed_d2, axis=1)

                    # Normalize KP locations to be independant from layers
                    KP_min_d2 = KP_min_d2 / (KP_extent**2)

                    # Loss will be the square distance to closest input point.
                    # Mean => loss independent from the number of input points
                    offset_losses += [tf.reduce_mean(KP_min_d2)]

                if op.name.endswith('deformed_KP'):

                    # Get deformed positions
                    deformed_KP = op.outputs[0]

                    # Layer index
                    layer = int(op.name.split('/')[1].split('_')[-1])

                    # Radius of deformed convolution for this layer
                    KP_extent = cfg.first_subsampling_dl * cfg.KP_extent * (
                        2**layer)

                    # Normalized KP locations
                    KP_locs = deformed_KP / KP_extent

                    # Point should not be close to each other
                    for i in range(cfg.num_kernel_points):
                        other_KP = tf.stop_gradient(
                            tf.concat(
                                [KP_locs[:, :i, :], KP_locs[:, i + 1:, :]],
                                axis=1))
                        distances = tf.sqrt(
                            tf.reduce_sum(tf.square(other_KP -
                                                    KP_locs[:, i:i + 1, :]),
                                          axis=2))
                        repulsive_losses = tf.reduce_sum(tf.square(
                            tf.maximum(0.0, 1.5 - distances)),
                                                         axis=1)
                        offset_losses += [tf.reduce_mean(repulsive_losses)]

        elif self.cfg.offsets_loss != 'none':
            raise ValueError('Unknown offset loss')

        if len(offset_losses) > 0:
            self.offsets_loss = self.cfg.offsets_decay * tf.add_n(
                offset_losses)
        else:
            self.offsets_loss = tf.constant(0, dtype=tf.float32)

        return self.offsets_loss + self.gaussian_loss + self.regularization_loss

    def parameters_log(self):

        self.cfg.save(self.saving_path)

    def get_batch_inds(self, stacks_len):
        """
        Method computing the batch indices of all points, given the batch element sizes (stack lengths). Example:
        From [3, 2, 5], it would return [0, 0, 0, 1, 1, 2, 2, 2, 2, 2]
        """

        # Initiate batch inds tensor
        num_batches = tf.shape(stacks_len)[0]
        num_points = tf.reduce_sum(stacks_len)
        batch_inds_0 = tf.zeros((num_points, ), dtype=tf.int32)

        # Define body of the while loop
        def body(batch_i, point_i, b_inds):

            num_in = stacks_len[batch_i]
            num_before = tf.cond(tf.less(batch_i, 1), lambda: tf.zeros(
                (), dtype=tf.int32),
                                 lambda: tf.reduce_sum(stacks_len[:batch_i]))
            num_after = tf.cond(
                tf.less(batch_i, num_batches - 1),
                lambda: tf.reduce_sum(stacks_len[batch_i + 1:]),
                lambda: tf.zeros((), dtype=tf.int32))

            # Update current element indices
            inds_before = tf.zeros((num_before, ), dtype=tf.int32)
            inds_in = tf.fill((num_in, ), batch_i)
            inds_after = tf.zeros((num_after, ), dtype=tf.int32)
            n_inds = tf.concat([inds_before, inds_in, inds_after], axis=0)

            b_inds += n_inds

            # Update indices
            point_i += stacks_len[batch_i]
            batch_i += 1

            return batch_i, point_i, b_inds

        def cond(batch_i, point_i, b_inds):
            return tf.less(batch_i, tf.shape(stacks_len)[0])

        _, _, batch_inds = tf.while_loop(cond,
                                         body,
                                         loop_vars=[0, 0, batch_inds_0],
                                         shape_invariants=[
                                             tf.TensorShape([]),
                                             tf.TensorShape([]),
                                             tf.TensorShape([None])
                                         ])

        return batch_inds

    def stack_batch_inds(self, stacks_len):

        # Initiate batch inds tensor
        num_points = tf.reduce_sum(stacks_len)
        max_points = tf.reduce_max(stacks_len)
        batch_inds_0 = tf.zeros((0, max_points), dtype=tf.int32)

        # Define body of the while loop
        def body(batch_i, point_i, b_inds):

            # Create this element indices
            element_inds = tf.expand_dims(tf.range(
                point_i, point_i + stacks_len[batch_i]),
                                          axis=0)

            # Pad to right size
            padded_inds = tf.pad(
                element_inds, [[0, 0], [0, max_points - stacks_len[batch_i]]],
                "CONSTANT",
                constant_values=num_points)

            # Concatenate batch indices
            b_inds = tf.concat((b_inds, padded_inds), axis=0)

            # Update indices
            point_i += stacks_len[batch_i]
            batch_i += 1

            return batch_i, point_i, b_inds

        def cond(batch_i, point_i, b_inds):
            return tf.less(batch_i, tf.shape(stacks_len)[0])

        fixed_shapes = [
            tf.TensorShape([]),
            tf.TensorShape([]),
            tf.TensorShape([None, None])
        ]
        _, _, batch_inds = tf.while_loop(cond,
                                         body,
                                         loop_vars=[0, 0, batch_inds_0],
                                         shape_invariants=fixed_shapes)

        # Add a last column with shadow neighbor if there is not
        def f1():
            return tf.pad(batch_inds, [[0, 0], [0, 1]],
                          "CONSTANT",
                          constant_values=num_points)

        def f2():
            return batch_inds

        batch_inds = tf.cond(tf.equal(num_points,
                                      max_points * tf.shape(stacks_len)[0]),
                             true_fn=f1,
                             false_fn=f2)

        return batch_inds

    def augment_input(self, stacked_points, batch_inds):

        cfg = self.cfg
        # Parameter
        num_batches = batch_inds[-1] + 1

        ##########
        # Rotation
        ##########

        if cfg.augment_rotation == 'vertical':

            # Choose a random angle for each element
            theta = tf.random.uniform((num_batches, ),
                                      minval=0,
                                      maxval=2 * np.pi)

            # Rotation matrices
            c, s = tf.cos(theta), tf.sin(theta)
            cs0 = tf.zeros_like(c)
            cs1 = tf.ones_like(c)
            R = tf.stack([c, -s, cs0, s, c, cs0, cs0, cs0, cs1], axis=1)
            R = tf.reshape(R, (-1, 3, 3))

            # Create N x 3 x 3 rotation matrices to multiply with stacked_points
            stacked_rots = tf.gather(R, batch_inds)

            # Apply rotations
            stacked_points = tf.reshape(
                tf.matmul(tf.expand_dims(stacked_points, axis=1),
                          stacked_rots), [-1, 3])

        elif cfg.augment_rotation == 'none':
            R = tf.eye(3, batch_shape=(num_batches, ))

        else:
            raise ValueError('Unknown rotation augmentation : ' +
                             cfg.augment_rotation)

        #######
        # Scale
        #######

        # Choose random scales for each example
        min_s = cfg.augment_scale_min
        max_s = cfg.augment_scale_max

        if cfg.augment_scale_anisotropic:
            s = tf.random.uniform((num_batches, 3), minval=min_s, maxval=max_s)
        else:
            s = tf.random.uniform((num_batches, 1), minval=min_s, maxval=max_s)

        symmetries = []
        for i in range(3):
            if cfg.augment_symmetries[i]:
                symmetries.append(
                    tf.round(tf.random.uniform((num_batches, 1))) * 2 - 1)
            else:
                symmetries.append(tf.ones([num_batches, 1], dtype=tf.float32))
        s *= tf.concat(symmetries, 1)

        # Create N x 3 vector of scales to multiply with stacked_points
        stacked_scales = tf.gather(s, batch_inds)

        # Apply scales
        stacked_points = stacked_points * stacked_scales

        #######
        # Noise
        #######

        noise = tf.random.normal(tf.shape(stacked_points),
                                 stddev=cfg.augment_noise)
        stacked_points = stacked_points + noise

        return stacked_points, s, R

    def segmentation_inputs(self,
                            stacked_points,
                            stacked_features,
                            point_labels,
                            stacks_lengths,
                            batch_inds,
                            object_labels=None):

        cfg = self.cfg
        # Batch weight at each point for loss (inverse of stacks_lengths for each point)
        min_len = tf.reduce_min(stacks_lengths, keepdims=True)
        batch_weights = tf.cast(min_len, tf.float32) / tf.cast(
            stacks_lengths, tf.float32)
        stacked_weights = tf.gather(batch_weights, batch_inds)

        # KPConv specific parameters
        density_parameter = 5.0

        # Starting radius of convolutions
        r_normal = cfg.first_subsampling_dl * cfg.KP_extent * 2.5

        # Starting layer
        layer_blocks = []

        # Lists of inputs
        input_points = []
        input_neighbors = []
        input_pools = []
        input_upsamples = []
        input_batches_len = []

        ######################
        # Loop over the blocks
        ######################

        for block_i, block in enumerate(cfg.architecture):

            # Stop when meeting a global pooling or upsampling
            if 'global' in block or 'upsample' in block:
                break

            # Get all blocks of the layer
            if not ('pool' in block or 'strided' in block):
                layer_blocks += [block]
                if block_i < len(cfg.architecture) - 1 and not (
                        'upsample' in cfg.architecture[block_i + 1]):
                    continue

            # Convolution neighbors indices
            # *****************************

            if layer_blocks:
                # Convolutions are done in this layer, compute the neighbors with the good radius
                if np.any(['deformable' in blck
                           for blck in layer_blocks[:-1]]):
                    r = r_normal * density_parameter / (cfg.KP_extent * 2.5)
                else:
                    r = r_normal
                conv_i = tf_batch_neighbors(stacked_points, stacked_points,
                                            stacks_lengths, stacks_lengths, r)
            else:
                # This layer only perform pooling, no neighbors required
                conv_i = tf.zeros((0, 1), dtype=tf.int32)

            # Pooling neighbors indices
            # *************************

            # If end of layer is a pooling operation
            if 'pool' in block or 'strided' in block:

                # New subsampling length
                dl = 2 * r_normal / (cfg.KP_extent * 2.5)

                # Subsampled points
                pool_p, pool_b = tf_batch_subsampling(stacked_points,
                                                      stacks_lengths,
                                                      sampleDl=dl)

                # Radius of pooled neighbors
                if 'deformable' in block:
                    r = r_normal * density_parameter / (cfg.KP_extent * 2.5)
                else:
                    r = r_normal

                # Subsample indices
                pool_i = tf_batch_neighbors(pool_p, stacked_points, pool_b,
                                            stacks_lengths, r)

                # Upsample indices (with the radius of the next layer to keep wanted density)
                up_i = tf_batch_neighbors(stacked_points, pool_p,
                                          stacks_lengths, pool_b, 2 * r)

            else:
                # No pooling in the end of this layer, no pooling indices required
                pool_i = tf.zeros((0, 1), dtype=tf.int32)
                pool_p = tf.zeros((0, 3), dtype=tf.float32)
                pool_b = tf.zeros((0, ), dtype=tf.int32)
                up_i = tf.zeros((0, 1), dtype=tf.int32)

            # Reduce size of neighbors matrices by eliminating furthest point
            # TODO :
            conv_i = self.big_neighborhood_filter(conv_i, len(input_points))
            pool_i = self.big_neighborhood_filter(pool_i, len(input_points))
            up_i = self.big_neighborhood_filter(up_i, len(input_points))

            # Updating input lists
            input_points += [stacked_points]
            input_neighbors += [conv_i]
            input_pools += [pool_i]
            input_upsamples += [up_i]
            input_batches_len += [stacks_lengths]

            # New points for next layer
            stacked_points = pool_p
            stacks_lengths = pool_b

            # Update radius and reset blocks
            r_normal *= 2
            layer_blocks = []

        ###############
        # Return inputs
        ###############

        # Batch unstacking (with last layer indices for optionnal classif loss)
        stacked_batch_inds_0 = self.stack_batch_inds(input_batches_len[0])

        # Batch unstacking (with last layer indices for optionnal classif loss)
        stacked_batch_inds_1 = self.stack_batch_inds(input_batches_len[-1])

        if object_labels is None:

            # list of network inputs
            li = input_points + input_neighbors + input_pools + input_upsamples
            li += [
                stacked_features, stacked_weights, stacked_batch_inds_0,
                stacked_batch_inds_1
            ]
            li += [point_labels]

            return li

        else:

            # Object class ind for each point
            stacked_object_labels = tf.gather(object_labels, batch_inds)

            # list of network inputs
            li = input_points + input_neighbors + input_pools + input_upsamples
            li += [
                stacked_features, stacked_weights, stacked_batch_inds_0,
                stacked_batch_inds_1
            ]
            li += [point_labels, stacked_object_labels]

            return li

    def transform(self, stacked_points, stacked_colors, point_labels,
                  stacks_lengths, point_inds, cloud_inds):
        """
        [None, 3], [None, 3], [None], [None]
        """
        cfg = self.cfg
        # Get batch indice for each point
        batch_inds = self.get_batch_inds(stacks_lengths)

        # Augment input points
        stacked_points, scales, rots = self.augment_input(
            stacked_points, batch_inds)

        # First add a column of 1 as feature for the network to be able to learn 3D shapes
        stacked_features = tf.ones((tf.shape(stacked_points)[0], 1),
                                   dtype=tf.float32)

        # Get coordinates and colors
        stacked_original_coordinates = stacked_colors[:, 3:]
        stacked_colors = stacked_colors[:, :3]

        # Augmentation : randomly drop colors
        if cfg.in_features_dim in [4, 5]:
            num_batches = batch_inds[-1] + 1
            s = tf.cast(
                tf.less(tf.random.uniform((num_batches, )), cfg.augment_color),
                tf.float32)
            stacked_s = tf.gather(s, batch_inds)
            stacked_colors = stacked_colors * tf.expand_dims(stacked_s, axis=1)

        # Then use positions or not
        if cfg.in_features_dim == 1:
            pass
        elif cfg.in_features_dim == 2:
            stacked_features = tf.concat(
                (stacked_features, stacked_original_coordinates[:, 2:]),
                axis=1)
        elif cfg.in_features_dim == 3:
            stacked_features = stacked_colors
        elif cfg.in_features_dim == 4:
            stacked_features = tf.concat((stacked_features, stacked_colors),
                                         axis=1)
        elif cfg.in_features_dim == 5:
            stacked_features = tf.concat((stacked_features, stacked_colors,
                                          stacked_original_coordinates[:, 2:]),
                                         axis=1)
        elif cfg.in_features_dim == 7:
            stacked_features = tf.concat(
                (stacked_features, stacked_colors, stacked_points), axis=1)
        else:
            raise ValueError(
                'Only accepted input dimensions are 1, 3, 4 and 7 (without and with rgb/xyz)'
            )

        # Get the whole input list
        input_list = self.segmentation_inputs(stacked_points, stacked_features,
                                              point_labels, stacks_lengths,
                                              batch_inds)

        # Add scale and rotation for testing
        input_list += [scales, rots]
        input_list += [point_inds, cloud_inds]

        return input_list

    def preprocess(self, data, attr):
        cfg = self.cfg
        if 'feat' not in data.keys():
            data['feat'] = None

        points = data['point'][:, 0:3]
        feat = data['feat'][:, 0:3]
        labels = data['label']
        split = attr['split']

        if (feat is None):
            sub_feat = None

        data = dict()

        if (feat is None):
            sub_points, sub_labels = DataProcessing.grid_sub_sampling(
                points, labels=labels, grid_size=cfg.first_subsampling_dl)

        else:
            sub_points, sub_feat, sub_labels = DataProcessing.grid_sub_sampling(
                points,
                features=feat,
                labels=labels,
                grid_size=cfg.first_subsampling_dl)

        search_tree = KDTree(sub_points)

        data['point'] = np.array(sub_points)
        data['feat'] = np.array(sub_feat)
        data['label'] = np.array(sub_labels)
        data['search_tree'] = search_tree

        if split != "training":
            proj_inds = np.squeeze(
                search_tree.query(points, return_distance=False))
            proj_inds = proj_inds.astype(np.int32)
            data['proj_inds'] = proj_inds

        return data

    def crop_pc(self, points, feat, labels, search_tree, pick_idx):
        # crop a fixed size point cloud for training
        num_points = 65536
        if (points.shape[0] < num_points):
            select_idx = np.array(range(points.shape[0]))
            diff = num_points - points.shape[0]
            select_idx = list(select_idx) + list(
                random.choices(select_idx, k=diff))
            random.shuffle(select_idx)
        else:
            center_point = points[pick_idx, :].reshape(1, -1)
            select_idx = search_tree.query(center_point, k=num_points)[1][0]

        # select_idx = DataProcessing.shuffle_idx(select_idx)
        random.shuffle(select_idx)
        select_points = points[select_idx]
        select_labels = labels[select_idx]
        if (feat is None):
            select_feat = None
        else:
            select_feat = feat[select_idx]
        return select_points, select_feat, select_labels, select_idx

    def get_batch_gen(self, dataset):

        cfg = self.cfg

        def spatially_regular_gen():

            random_pick_n = None
            epoch_n = cfg.epoch_steps * cfg.batch_num
            split = dataset.split

            batch_limit = 5000  # TODO : read from calibrate_batch, typically 100 * batch_size required

            # Initiate potentials for regular generation
            if not hasattr(self, 'potentials'):
                self.potentials = {}
                self.min_potentials = {}

            # Reset potentials
            self.potentials[split] = []
            self.min_potentials[split] = []
            data_split = split

            #TODO :
            # for i, tree in enumerate(self.input_trees[data_split]):
            #     self.potentials[split] += [np.random.rand(tree.data.shape[0]) * 1e-3]
            #     self.min_potentials[split] += [float(np.min(self.potentials[split][-1]))]

            # Initiate concatanation lists
            p_list = []
            c_list = []
            pl_list = []
            pi_list = []
            ci_list = []

            batch_n = 0

            # Generator loop
            for i in range(epoch_n):
                # Choose a random cloud
                # cloud_ind = int(np.argmin(self.min_potentials[split]))
                cloud_ind = random.randint(0, dataset.num_pc - 1)

                data, attr = dataset.read_data(cloud_ind)

                # Choose point ind as minimum of potentials
                # point_ind = np.argmin(self.potentials[split][cloud_ind])
                point_ind = np.random.choice(len(data['point']), 1)

                # Get points from tree structure
                # points = np.array(self.input_trees[data_split][cloud_ind].data, copy=False)
                points = np.array(data['search_tree'].data, copy=False)

                # Center point of input region
                center_point = points[point_ind, :].reshape(1, -1)
                # Add noise to the center point
                # if split != 'ERF':
                #     noise = np.random.normal(scale=cfg.in_radius/10, size=center_point.shape)
                #     pick_point = center_point + noise.astype(center_point.dtype)
                # else:
                #     pick_point = center_point
                pick_point = center_point

                # Indices of points in input region
                # input_inds = self.input_trees[data_split][cloud_ind].query_radius(pick_point,
                #                                                                 r=cfg.in_radius)[0]
                input_inds = data['search_tree'].query_radius(
                    pick_point, r=cfg.in_radius)[0]

                # Number collected
                n = input_inds.shape[0]

                # Update potentials (Tuckey weights)
                # if split != 'ERF':
                #     dists = np.sum(np.square((points[input_inds] - pick_point).astype(np.float32)), axis=1)
                #     tukeys = np.square(1 - dists / np.square(in_radius))
                #     tukeys[dists > np.square(in_radius)] = 0
                #     self.potentials[split][cloud_ind][input_inds] += tukeys
                #     self.min_potentials[split][cloud_ind] = float(np.min(self.potentials[split][cloud_ind]))

                # Safe check for very dense areas
                if n > batch_limit:
                    input_inds = np.random.choice(input_inds,
                                                  size=int(batch_limit) - 1,
                                                  replace=False)
                    n = input_inds.shape[0]

                # Collect points and colors
                input_points = (points[input_inds] - pick_point).astype(
                    np.float32)
                # input_colors = self.input_colors[data_split][cloud_ind][input_inds]
                input_colors = data['feat'][input_inds]

                if split in ['test']:
                    input_labels = np.zeros(input_points.shape[0])
                else:
                    # input_labels = self.input_labels[data_split][cloud_ind][input_inds]
                    input_labels = data['label'][input_inds][:, 0]
                    # input_labels = np.array([self.label_to_idx[l] for l in input_labels])

                # In case batch is full, yield it and reset it
                if batch_n + n > batch_limit and batch_n > 0:

                    yield (np.concatenate(p_list, axis=0),
                           np.concatenate(c_list, axis=0),
                           np.concatenate(pl_list, axis=0),
                           np.array([tp.shape[0] for tp in p_list]),
                           np.concatenate(pi_list, axis=0),
                           np.array(ci_list, dtype=np.int32))

                    p_list = []
                    c_list = []
                    pl_list = []
                    pi_list = []
                    ci_list = []
                    batch_n = 0

                # Add data to current batch
                if n > 0:
                    p_list += [input_points]
                    c_list += [
                        np.hstack((input_colors, input_points + pick_point))
                    ]
                    pl_list += [input_labels]
                    pi_list += [input_inds]
                    ci_list += [cloud_ind]

                # Update batch size
                batch_n += n

            if batch_n > 0:
                yield (np.concatenate(p_list,
                                      axis=0), np.concatenate(c_list, axis=0),
                       np.concatenate(pl_list, axis=0),
                       np.array([tp.shape[0] for tp in p_list]),
                       np.concatenate(pi_list,
                                      axis=0), np.array(ci_list,
                                                        dtype=np.int32))

        gen_func = spatially_regular_gen
        gen_types = (tf.float32, tf.float32, tf.int32, tf.int32, tf.int32,
                     tf.int32)
        gen_shapes = ([None, 3], [None, 6], [None], [None], [None], [None])

        return gen_func, gen_types, gen_shapes

