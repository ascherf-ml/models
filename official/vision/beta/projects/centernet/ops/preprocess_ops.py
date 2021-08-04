# Copyright 2021 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import tensorflow as tf

from official.vision.beta.projects.yolo.ops import box_ops

LARGE_NUM = 1. / tf.keras.backend.epsilon()


def _smallest_positive_root(a, b, c):
  """Returns the smallest positive root of a quadratic equation."""

  discriminant = tf.sqrt(b ** 2 - 4 * a * c)

  # TODO(vighneshb) We are currently using the slightly incorrect
  # CenterNet implementation. The commented lines implement the fixed version
  # in https://github.com/princeton-vl/CornerNet. Change the implementation
  # after verifying it has no negative impact.
  # root1 = (-b - discriminant) / (2 * a)
  # root2 = (-b + discriminant) / (2 * a)

  # return tf.where(tf.less(root1, 0), root2, root1)

  return (-b + discriminant) / (2.0)


def gaussian_radius(det_size, min_overlap=0.7) -> int:
  """
    Given a bounding box size, returns a lower bound on how far apart the
    corners of another bounding box can lie while still maintaining the given
    minimum overlap, or IoU. Modified from implementation found in
    https://github.com/tensorflow/models/blob/master/research/object_detection/core/target_assigner.py.

    Params:
        det_size (tuple): tuple of integers representing height and width
        min_overlap (tf.float32): minimum IoU desired
    Returns:
        int representing desired gaussian radius
    """
  height, width = det_size[0], det_size[1]
  
  # Case where detected box is offset from ground truth and no box completely
  # contains the other.
  
  a1 = 1
  b1 = -(height + width)
  c1 = width * height * (1 - min_overlap) / (1 + min_overlap)
  r1 = _smallest_positive_root(a1, b1, c1)
  
  # Case where detection is smaller than ground truth and completely contained
  # in it.
  
  a2 = 4
  b2 = -2 * (height + width)
  c2 = (1 - min_overlap) * width * height
  r2 = _smallest_positive_root(a2, b2, c2)
  
  # Case where ground truth is smaller than detection and completely contained
  # in it.
  
  a3 = 4 * min_overlap
  b3 = 2 * min_overlap * (height + width)
  c3 = (min_overlap - 1) * width * height
  r3 = _smallest_positive_root(a3, b3, c3)
  # TODO discuss whether to return scalar or tensor
  
  return tf.reduce_min([r1, r2, r3], axis=0)


def _gaussian_penalty(radius: int, dtype=tf.float32) -> tf.Tensor:
  """
  This represents the penalty reduction around a point.
  Params:
      radius (int): integer for radius of penalty reduction
      type (tf.dtypes.DType): datatype of returned tensor
  Returns:
      tf.Tensor of shape (2 * radius + 1, 2 * radius + 1).
  """
  width = 2 * radius + 1
  sigma = tf.cast(radius / 3, dtype=dtype)
  
  range_width = tf.range(width)
  range_width = tf.cast(range_width - tf.expand_dims(radius, axis=-1),
                        dtype=dtype)
  
  x = tf.expand_dims(range_width, axis=-1)
  y = tf.expand_dims(range_width, axis=-2)
  
  exponent = ((-1 * (x ** 2) - (y ** 2)) / (2 * sigma ** 2))
  return tf.math.exp(exponent)


@tf.function
def cartesian_product(*tensors, repeat=1):
  """
  Equivalent of itertools.product except for TensorFlow tensors.

  Example:
    cartesian_product(tf.range(3), tf.range(4))

    array([[0, 0],
       [0, 1],
       [0, 2],
       [0, 3],
       [1, 0],
       [1, 1],
       [1, 2],
       [1, 3],
       [2, 0],
       [2, 1],
       [2, 2],
       [2, 3]], dtype=int32)>

  Params:
    tensors (list[tf.Tensor]): a list of 1D tensors to compute the product of
    repeat (int): number of times to repeat the tensors
      (https://docs.python.org/3/library/itertools.html#itertools.product)

  Returns:
    An nD tensor where n is the number of tensors
  """
  tensors = tensors * repeat
  return tf.reshape(tf.transpose(tf.stack(tf.meshgrid(*tensors, indexing='ij')),
                                 [*[i + 1 for i in range(len(tensors))], 0]),
                    (-1, len(tensors)))


@tf.function
def draw_gaussian(hm_shape, blob, dtype, scaling_factor=1):
  """ Draws an instance of a 2D gaussian on a heatmap.

  A heatmap with shape hm_shape and of type dtype is generated with
  a gaussian with a given center, radius, and scaling factor

  Args:
    hm_shape: A `list` of `Tensor` of shape [3] that gives the height, width,
      and number of channels in the heatmap
    blob: A `Tensor` of shape [4] that gives the channel number, x, y, and
      radius for the desired gaussian to be drawn onto
    dtype: The desired type of the heatmap
    scaling_factor: A `int` that can be used to scale the magnitude of the
      gaussian
  Returns:
    A `Tensor` with shape hm_shape and type dtype with a 2D gaussian
  """
  gaussian_heatmap = tf.zeros(shape=hm_shape, dtype=dtype)
  
  blob = tf.cast(blob, tf.int32)
  obj_class, x, y, radius = blob[0], blob[1], blob[2], blob[3]
  
  height, width = hm_shape[0], hm_shape[1]
  
  left = tf.math.minimum(x, radius)
  right = tf.math.minimum(width - x, radius + 1)
  top = tf.math.minimum(y, radius)
  bottom = tf.math.minimum(height - y, radius + 1)
  
  gaussian = _gaussian_penalty(radius=radius, dtype=dtype)
  gaussian = gaussian[radius - top:radius + bottom, radius - left:radius + right]
  gaussian = tf.reshape(gaussian, [-1])
  
  heatmap_indices = cartesian_product(
      tf.range(y - top, y + bottom), tf.range(x - left, x + right), [obj_class])
  gaussian_heatmap = tf.tensor_scatter_nd_update(
      gaussian_heatmap, heatmap_indices, gaussian * scaling_factor)
  
  return gaussian_heatmap


def get_image_shape(image):
  shape = tf.shape(image)
  if tf.shape(shape)[0] == 4:
    width = shape[2]
    height = shape[1]
  else:
    width = shape[1]
    height = shape[0]
  return height, width


def letter_box(image, boxes, xs=0.5, ys=0.5, target_dim=None):
  height, width = get_image_shape(image)
  clipper = tf.math.maximum(width, height)
  if target_dim is None:
    target_dim = clipper
  
  xs = tf.convert_to_tensor(xs)
  ys = tf.convert_to_tensor(ys)
  pad_width_p = clipper - width
  pad_height_p = clipper - height
  pad_height = tf.cast(tf.cast(pad_height_p, ys.dtype) * ys, tf.int32)
  pad_width = tf.cast(tf.cast(pad_width_p, xs.dtype) * xs, tf.int32)
  image = tf.image.pad_to_bounding_box(image, pad_height, pad_width,
                                       clipper, clipper)
  
  boxes = box_ops.yxyx_to_xcycwh(boxes)
  x, y, w, h = tf.split(boxes, 4, axis=-1)
  
  y *= tf.cast(height / clipper, y.dtype)
  x *= tf.cast(width / clipper, x.dtype)
  
  y += tf.cast((pad_height / clipper), y.dtype)
  x += tf.cast((pad_width / clipper), x.dtype)
  
  h *= tf.cast(height / clipper, h.dtype)
  w *= tf.cast(width / clipper, w.dtype)
  
  boxes = tf.concat([x, y, w, h], axis=-1)
  
  boxes = box_ops.xcycwh_to_yxyx(boxes)
  boxes = tf.where(h == 0, tf.zeros_like(boxes), boxes)
  
  image = tf.image.resize(image, (target_dim, target_dim))
  
  scale = target_dim / clipper
  pt_width = tf.cast(tf.cast(pad_width, scale.dtype) * scale, tf.int32)
  pt_height = tf.cast(tf.cast(pad_height, scale.dtype) * scale, tf.int32)
  pt_width_p = tf.cast(tf.cast(pad_width_p, scale.dtype) * scale, tf.int32)
  pt_height_p = tf.cast(tf.cast(pad_height_p, scale.dtype) * scale, tf.int32)
  return image, boxes, [pt_height, pt_width, target_dim - pt_height_p,
                        target_dim - pt_width_p]


def pad_max_instances(value, instances, pad_value=0, pad_axis=0):
  shape = tf.shape(value)
  if pad_axis < 0:
    pad_axis = tf.shape(shape)[0] + pad_axis
  dim1 = shape[pad_axis]
  take = tf.math.reduce_min([instances, dim1])
  value, _ = tf.split(value, [take, -1], axis=pad_axis)  # value[:instances, ...]
  pad = tf.convert_to_tensor([tf.math.reduce_max([instances - dim1, 0])])
  nshape = tf.concat([shape[:pad_axis], pad, shape[(pad_axis + 1):]], axis=0)
  pad_tensor = tf.fill(nshape, tf.cast(pad_value, dtype=value.dtype))
  value = tf.concat([value, pad_tensor], axis=pad_axis)
  return value