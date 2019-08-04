# -*- coding: utf8 -*-
import re
from copy import deepcopy

import attr
import numpy as np
import six
from attr.validators import instance_of

import tensorflow as tf
from tensorflow.core.framework.attr_value_pb2 import AttrValue as _AttrValue
from tensorflow.core.framework.attr_value_pb2 import \
    NameAttrList as _NameAttrList
from tensorflow.core.framework.tensor_pb2 import TensorProto as _TensorProto
from tensorflow.core.framework.tensor_shape_pb2 import \
    TensorShapeProto as _TensorShapeProto
from tensorflow.core.framework.types_pb2 import DataType as _DataType
from utensor_cgen.utils import random_str, topologic_order_graph

from .converter import AttrValueConverter, ConverterDispatcher

__all__ = [
  'TensorInfo', 'OperationInfo',
  'MetaOperationInfo', 'uTensorGraph',
  'uTensorGraphView'
]


class _NoShallowCopyMixin(object):

  def __copy__(self):
    raise RuntimeError('shallow copy is not allowed for type %s' % type(self))


class IRBase(object):

  @property
  def all_supported_backends(self):
    return ['tensorflow']


@attr.s(cmp=False)
class TensorInfo(IRBase, _NoShallowCopyMixin):
  """
  :param name: the name of the tensor
  :type name: six.string_types

  :param op_name: the name of the operator which generate
    this tensor
  :type op_name: six.string_types

  :param dtype: the data type of the elements.
  :type dtype: numpy.dtype

  :param shape: the shape of the tensor. Should be a list
    of integers or ``None``.
  :type shape: list

  :param ugraph: a ``uTensorGraph``, which this tensor belongs to.
    By passing an ``uTensorGraph`` object to the constructor, the
    tensor is `*owned*` by the graph
  :type ugraph: :class:`.uTensorGraph`
  """
  name = attr.ib(validator=instance_of(six.string_types))
  op_name = attr.ib(validator=instance_of(six.string_types))
  dtype = attr.ib(validator=instance_of(np.dtype))

  shape = attr.ib(validator=instance_of((list, type(None))))
  @shape.validator
  def check(self, attrib, shape_values):
    if shape_values is not None:
      for v in shape_values:
        assert isinstance(v, (int, type(None))), \
          "shape should be a list of integers"
          
  _ugraph = attr.ib(repr=False)
  @_ugraph.validator
  def check(self, attrib, value):
    if not isinstance(value, uTensorGraph):
      raise ValueError('Expecting a uTensorGraph, get {}'.format(type(value)))

  _NULL_PREFIX = 'utensor_null'

  def move_into(self, ugraph):
    """
    Move synmatic of the `TensorInfo` objects

    :meth:`move_info` will move the tensor to the given graph, 
    that is, transferring ownership of the tensor from original graph
    to other graph
    """
    self._ugraph = ugraph

  @classmethod
  def make_null_tensor(
    cls,
    ugraph,
    dtype=np.dtype('float'),
    shape=None
  ):
    """
    Make a null tensor

    A null tensor is a tensor comes from nowhere, that is,
    it is not generated by any node in the graph

    :param ugraph: the graph where to add the null tensor
    :type ugraph: utensor_cgen.ir.base.uTensorGraph

    :param dtype: the data type of the elements
    :type dtype: numpy.dtype

    :param shape: the shape of the tensor
    :type shape: list

    :rtype: :class:`utenosr.ir.base.TensorInfo`
    """
    op_name = '{}_{}'.format(cls._NULL_PREFIX, random_str())
    name = '{}:0'.format(op_name)
    return cls(
      name=name,
      op_name=op_name,
      dtype=dtype,
      shape=shape,
      ugraph=ugraph
    )
  
  @property
  def ugraph(self):
    """
    :class:`.uTensorGraph` which the tensor belongs to

    :rtype: :class:`.uTensorGraph`
    """
    return self._ugraph

  @property
  def op(self):
    """
    :class:`.OperationInfo` which generate this tensor
    
    `None` returned for null tensor, see :meth:`.make_null_tensor`

    :rtype: :class:`.OperationInfo` or `None`
    """
    op = self._ugraph.ops_info.get(self.op_name, None)
    if not op and not self.is_null_tensor:
      raise ValueError('Unknown op name: {}'.format(self.op_name))
    return op

  @property
  def backend(self):
    """
    the backend library/framework used for training
    the graph

    :rtype: six.string_types
    """
    return self._ugraph.backend
  
  @property
  def is_null_tensor(self):
    """
    whether the tensor is a null tensor or not

    :rtype: bool
    """
    return self.op_name.startswith(self._NULL_PREFIX)

  def __deepcopy__(self, memo):
    new_tensor = TensorInfo(name=self.name,
                            ugraph=memo['ugraph'],
                            op_name=self.op_name,
                            dtype=self.dtype,
                            shape=deepcopy(self.shape, memo))
    return new_tensor
  
  def __hash__(self):
    return hash(self.name)
  
  def __eq__(self, other):
    if not isinstance(other, type(self)):
      return False
    return (self.name == other.name) and (self._ugraph is other._ugraph)


@attr.s(cmp=False, repr=False)
class OperationInfo(IRBase, _NoShallowCopyMixin):
  """
  :param name: the name of the node
  :type name: str

  :param input_tensors: the input tensors of the node
  :type input_tensors: List[TensorInfo]

  :param output_tensors: the output tensors of the node
  :type output_tensors: List[TensorInfo]

  :param input_nodes: the nodes which generate the tensors in ``input_tensors``
  :type input_nodes: Set[OperationInfo]

  :param output_nodes: the nodes which consume the ``output_tensors`` in the graph
  :type output_nodes: Set[OperationInfo]

  :param op_type: the type of the node (ex: ``Add``)
  :type op_type: str

  :param backend: the name of the backend, the library/framework for the training phase
    {"tensorflow", 'pytorch'}
  :type backend: str

  :param op_attr: a dict containing extra information of this op
  :type op_attr: dict

  - **op_attr** is dictionary with key as str and value as generic
    types defined in :meth:`.ConverterFactor.all_generic_types`.
  - The only exception is the key which match regex pattern ``r'_[^_]*'``.
    That is, any name starts with single ``_``.

    - The values of such keys will be saved **as-is** without any type conversion.
  """
  name = attr.ib(type=str)
  _backend = attr.ib(type=str)
  # FIXME: it's better to make OperationInfo to be instantiate without ugraph
  _ugraph = attr.ib(repr=False)
  @_ugraph.validator
  def check(self, attrib, value):
    if not isinstance(value, uTensorGraph):
      raise ValueError(
        'Expecting a uTensorGraph, '
        'get {}'.format(type(value))
      )

  input_tensors = attr.ib(validator=instance_of(list))
  @input_tensors.validator
  def check(self, attribute, value):
    # FIXME: we need a refactor of IR, allowing None here is just temporary
    # especially for graph rewrite
    if not all([isinstance(v, (TensorInfo, type(None))) for v in value]):
      raise ValueError('Expecting a list of TensorInfo for input_tensors')

  output_tensors = attr.ib(validator=instance_of(list))
  @output_tensors.validator
  def check(self, attribute, value):
    if not all([isinstance(v, TensorInfo) for v in value]):
      raise ValueError('Expecting a list of TensorInfo for output_tensors')

  op_type = attr.ib(type=str)

  op_attr = attr.ib(factory=dict, converter=dict)

  n_inputs = attr.ib()
  @n_inputs.default
  def default_n_inputs(self):
    return len(self.input_tensors)

  n_outputs = attr.ib()
  @n_outputs.default
  def default_n_outputs(self):
    return len(self.output_tensors)

  def __attrs_post_init__(self):
    skip_pattern = re.compile(r'_utensor_[^_]*')
    if self.op_attr:
      op_attr = {}
      for k, v in self.op_attr.items():
        match = skip_pattern.match(k)
        if match:
          op_attr[k] = v
        else:
          op_attr[k] = ConverterDispatcher.get_generic_value(v)
      self.op_attr = op_attr
    self._ugraph.ops_info[self.name] = self
    if not self.n_inputs == len(self.input_tensors):
      raise ValueError(
        'n_inputs is not equal to the length of input_tensors: {}'.format(self.name)
      )
    if not self.n_outputs == len(self.output_tensors):
      raise ValueError(
        'n_outputs is not equal to the length of output_tensors: {}'.format(self.name)
      )

  @property
  def ugraph(self):
    return self._ugraph
  
  @property
  def backend(self):
    return self._backend

  @property
  def input_nodes(self):
    in_ops = []
    for tensor in self.input_tensors:
      if tensor.op is None:
        continue
      if tensor.op_name not in in_ops:
        in_ops.append(tensor.op_name)
    return [self._ugraph.ops_info.get(name, None) for name in in_ops]

  @property
  def output_nodes(self):
    out_ops = []
    for op in self._ugraph.ops:
      for in_tensor in op.input_tensors:
        if in_tensor.op_name == self.name and op.name not in out_ops:
          out_ops.append(op.name)
          break
    return [self._ugraph.ops_info[name] for name in out_ops]

  def add_null_input_tensor(self, idx=-1):
    if self.op_type != 'Placeholder':
      raise ValueError(
        'can only add null tensor to op of type Placeholder: %s' % self.op_type
      )
    if idx > len(self.input_tensors):
      raise ValueError(
        "can't insert null tensor at {} as {} input tensors present".format(
          idx, len(self,input_tensors)
        )
      )
    null_tensor = TensorInfo.make_null_tensor(ugraph=self._ugraph)
    self.input_tensors.insert(idx, null_tensor)
    self.n_inputs += 1
    return null_tensor
  
  def replace_with_null_input_tensor(self, idx):
    if idx >= len(self.input_tensors):
      raise ValueError(
        'index out of bound: %s' % idx
      )
    self.input_tensors[idx] = TensorInfo.make_null_tensor(ugraph=self._ugraph)

  def move_into(self, ugraph):
    """
    Move Synmatic of the OperationInfo objects
    """
    self._ugraph = ugraph
    for tensor in self.input_tensors:
      tensor.move_into(ugraph)
    for tensor in self.output_tensors:
      tensor.move_into(ugraph)
    ugraph.ops_info[self.name] = self
  
  def __deepcopy__(self, memo):
    op_info = OperationInfo(name=self.name,
                            input_tensors=deepcopy(self.input_tensors, memo),
                            n_inputs=self.n_inputs,
                            output_tensors=deepcopy(self.output_tensors, memo),
                            n_outputs=self.n_outputs,
                            op_type=self.op_type,
                            backend=self.backend,
                            op_attr=deepcopy(self.op_attr, memo),
                            ugraph=memo['ugraph'])
    return op_info

  def __hash__(self):
    return hash(self.name)
  
  def __eq__(self, other):
    if not isinstance(other, type(self)):
      return False
    return (self.name == other.name) and (self._ugraph is other._ugraph)
  
  def __getitem__(self, tensor_idx):
    num_out_tensors = len(self.output_tensors)
    if tensor_idx > num_out_tensors:
      raise IndexError(
        'index out of bound: {} out of {}'.format(tensor_idx, num_out_tensors)
      )
    return self.output_tensors[tensor_idx]

  def __repr__(self):
    return str((self.name, self.op_type))


@attr.s(cmp=False)
class uTensorGraph(IRBase, _NoShallowCopyMixin):
  """
  :param output_nodes: a list of names of ops which are the output nodes
    in the graph
  :type output_nodes: list
  :param ops_info: a dict with key as string, the op's name,
    and value as an instance of :class:`.OperationInfo`
  :type ops_info: dict
  :param backend: the name of backend library/framework
    the graph trained with. Can only be ``'tensorflow'``
    or ``'pytorch'`` (future work)
  :type backend: str

  ..

    NOTE:

    - **topo_order** is a non-init attribute which is set according to
      given **ops_info** and **output_nodes**. It will be a list of op
      names in topological sorting order
    - How to build a :class:`.uTensorGraph`

      1. create a empty graph

        - give a list of names of output nodes (required)
        - (optional) give backend string
        - leave ops_info empty
      2. setup the ops_info

        - when you set the value of ops_info, which is an OperationInfo instance,
          make sure its ugraph attribute is the ugraph you just created at step 1
      3. pass the ugraph to :func:`.topologic_order_graph` to setup the order of the ops

  """
  KWPARSER_PATTERN = re.compile(r'^([^\d\W][\w\d_]*)__([^\d\W][\w\d_]*)')

  output_nodes = attr.ib(type=list)
  _backend = attr.ib(default='', type=six.string_types)
  ops_info = attr.ib(factory=dict)
  # non-init
  topo_order = attr.ib(factory=list, init=False)
  _type_to_op_map = attr.ib(factory=dict, init=False, repr=False)

  def __attrs_post_init__(self):
    if not self.output_nodes:
      raise ValueError('No output_nodes given')
  
  def get_ops_by_type(self, given_op_type):
    """
    :param given_op_type: the op_type to search with
    :type given_op_type: six.string_types

    :rtype: :class:`.OperationInfo`
    """
    if not self._type_to_op_map:
      for op_info in self.ops_info.values():
        op_type = op_info.op_type
        ops = self._type_to_op_map.get(
          op_type,
          []
        ) + [op_info]
        self._type_to_op_map.update(
          [(op_type, ops),]
        )
    return self._type_to_op_map.get(given_op_type, [])
  
  @property
  def output_ops(self):
    """
    list of output nodes

    :rtype: List[:class:`.OperationInfo`]
    """
    return [self.ops_info[name] for name in self.output_nodes]
  
  @property
  def output_tensors(self):
    """
    list of output tensors

    :rtype: List[:class:`.TensorInfo`]
    """
    out_tensors = set([])
    for op in self.output_ops:
      for tensor in op.output_tensors:
        out_tensors.add(tensor)
    return out_tensors

  @property
  def input_ops(self):
    """
    list of input nodes

    a node is considered as input node iff any
    one of following condition is true

      1. it takes no input tensor
      2. one of its input tensors is a null tensor

    :rtype: List[:class:`.OperationInfo`]
    """
    ops = []
    for op in self.ops_info.values():
      if (
        not op.input_tensors 
        or any([tensor.is_null_tensor for tensor in op.input_tensors])
      ):
        ops.append(op)
    return ops
  
  @property
  def input_tensors(self):
    """
    list of input tensors

    a tensor is an input tensor iff its op is
    listed in `input_ops <#utensor_cgen.ir.base.uTensorGraph.input_ops>`_

    :rtype: List[:class:`.TensorInfo`]
    """
    in_tensors = set([])
    in_op_names = set(op.name for op in self.input_ops)
    for op in self.input_ops:
      in_tensors.update(
        [
          tensor for tensor in op.input_tensors
          if tensor.op_name not in in_op_names
        ]
      )
    return in_tensors
  
  @property
  def backend(self):
    """
    the name of backend library/framework, ex: tensorflow

    :rtype: six.strings_type
    """
    return self._backend

  @property
  def graph_def(self):
    """
    Dynamically generated :class:`tensorflow.GraphDef` object
    
    :rtype: tensorflow.GraphDef
    """
    assert self._backend == 'tensorflow', \
      'Convert a uTensorGraph to tf.GraphDef from a non-tf backend'
    graph_def = tf.GraphDef()
    for node_name in self.topo_order:
      op_info = self.ops_info[node_name]
      attr = {}
      for key, obj in op_info.op_attr.items():
        if self.KWPARSER_PATTERN.match(key):
          continue
        value_name = obj.value_name
        tf_value = ConverterDispatcher.get_tf_value(obj.value)
        attr_value = _AttrValue(**{value_name: tf_value})
        attr[key] = attr_value
      graph_def.node.add(name=op_info.name,
                         op=op_info.op_type,
                         input=[in_tensor.name for in_tensor in op_info.input_tensors],
                         device=op_info.op_attr.get('tensorflow__device', ''),
                         attr=attr)
    return graph_def
  
  @property
  def ops(self):
    if not self.topo_order:
      topologic_order_graph(self)
    return [self.ops_info[name] for name in self.topo_order]

  def add_op(self, op, sort=True):
    # experimental, don't use
    if not isinstance(op, OperationInfo):
      raise ValueError('expecting OperationInfo, get {}'.format(type(op)))
    if op.name in self.ops_info:
      raise ValueError('duplicate op detected, {}'.format(op.name))
    op._ugraph = self

    self.ops_info[op.name] = op

    # FIXME: forcing a topo-order here prevent us from dynamic-graph-construction
    # The temporary fix is to disable this as an option
    if sort:
      topologic_order_graph(self)

  def unsafe_merge_into(self, other_ugraph):
    """
    Merge this graph with other given graph (unsafe)

    :param other_ugraph: the other graph to merge into
    :type other_ugraph: :class:`.uTensorGraph`

    ..

      NOTE:(**IMPORTANT**)
      
      As the name suggest, this method is not safe.
      Whenever you make a method call, you should
      consider both the graph and the other ugraph
      are dangling, which means following attribute
      may not be valid:
      
      - **output_nodes**

        - you have to manually merge the **output_nodes** \
          of the two graphs
      - **topo_order**
      - **ops_info**
      
      You should fix **output_nodes** first before performing
      any other checks and fixs.
      
      As for **topo_order** and **ops_info**, you can make
      use of follwoing functions in module :ref:`utils`:
      
      1. :func:`prune_graph`: remove all ops that is not \
        needed for computing output tensors of output nodes
      2. :func:`topologic_order_graph`: it will fix **topo_order** \
        attribute of given graph *in-place*, given that **output_nodes** \
        is valid.
    """
    for op in self.ops_info.values():
      op.move_into(other_ugraph)
      if op.op_type not in self._type_to_op_map:
        self._type_to_op_map[op.op_type] = []
      self._type_to_op_map[op.op_type].append(op)

  def __deepcopy__(self, memo):
    new_graph = uTensorGraph(
      output_nodes=self.output_nodes,
      backend=self._backend
    )
    memo['ugraph'] = new_graph

    new_graph.ops_info = {
      k: deepcopy(v, memo)
      for k, v in self.ops_info.items()
    }
    topologic_order_graph(new_graph)
    return new_graph

  def __getitem__(self, op_name):
    if op_name not in self.ops_info:
      raise KeyError('{} not found in the graph'.format(op_name))
    return self.ops_info[op_name]

  # def drop_op(self, op_name):
  #   # DON'T USE
  #   if op_name not in self.ops_info:
  #     raise ValueError('op not found in the graph: {}'.format(op_name))
  #   del self.ops_info[op_name]
  #   self.topo_order.remove(op_name)


@attr.s(cmp=False)
class uTensorGraphView(IRBase, _NoShallowCopyMixin):

  _ugraph = attr.ib(type=uTensorGraph)
  _op_names = attr.ib(type=list)
  output_nodes = attr.ib(type=list)
  ops_info = attr.ib(init=False, factory=dict)

  def __attrs_post_init__(self):
    for name in self._op_names:
      self.ops_info[name] = self._ugraph.ops_info[name]
  
  @property
  def backend(self):
    return self._ugraph.backend

  @property
  def input_ops(self):
    ops = set([])
    for name in self.ops_info:
      op = self.ops_info[name]
      input_tensors = op.input_tensors
      if all([
        tensor.op.name not in self.ops_info
        for tensor in input_tensors
      ]):
        ops.add(op)
    return ops
  
  @property
  def input_tensors(self):
    in_tensors = []
    for op in self.input_ops:
      for tensor in op.input_tensors:
        in_tensors.append(tensor)
    return in_tensors
  
  @property
  def output_ops(self):
    return [self.ops_info[name] for name in self.output_nodes]
  
  @property
  def output_tensors(self):
    out_tensors = []
    for op in self.output_ops:
      for tensor in op.output_tensors:
        out_tensors.append(tensor)
    return out_tensors

  def __getitem__(self, op_name):
    if op_name not in self.ops_info:
      raise KeyError('{} not found in the graph view'.format(op_name))
    return self.ops_info[op_name]


class MetaOperationInfo(OperationInfo):

  def __init__(self, op_info, morphism):
    self._op_info = op_info
    self.morphism = morphism

  def __getattr__(self, name):
    return getattr(self._op_info, name)
