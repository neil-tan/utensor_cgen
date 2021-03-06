# -*- coding:utf8 -*-
r"""Graph Visualization Transformer

Transformers that outputs a image file presenting the ugraph before the transformation
"""
import re
from collections import defaultdict
from copy import deepcopy

from utensor_cgen.ir import OperationInfo, uTensorGraph
from utensor_cgen.utils import parse_tensor_name
from utensor_cgen.logger import logger
from utensor_cgen.ir.misc.graph_viz import viz_graph

from .base import Transformer

__all__ = ["GraphVizTransformer"]


class GraphVizTransformer(Transformer):
  METHOD_NAME = 'graph_viz'
  KWARGS_NAMESCOPE = '_utensor_graph_viz'

  def __init__(self, out_fname="graph.gv", view=False):
    self.out_fname = out_fname
    self.view = view
  
  def transform(self, ugraph):
    viz_graph(self.out_fname, self.view , ugraph)

    return ugraph