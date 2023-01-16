# Copyright (C) 2022 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#

import functools

from mmdet.models.builder import DETECTORS
from mmdet.models.detectors.mask_rcnn import MaskRCNN

from otx.mpa.deploy.utils import is_mmdeploy_enabled
from otx.mpa.modules.utils.task_adapt import map_class_names
from otx.mpa.utils.logger import get_logger

from .l2sp_detector_mixin import L2SPDetectorMixin
from .sam_detector_mixin import SAMDetectorMixin

logger = get_logger()


@DETECTORS.register_module()
class CustomMaskRCNN(SAMDetectorMixin, L2SPDetectorMixin, MaskRCNN):
    def __init__(self, *args, task_adapt=None, **kwargs):
        super().__init__(*args, **kwargs)

        # Hook for class-sensitive weight loading
        if task_adapt:
            self._register_load_state_dict_pre_hook(
                functools.partial(
                    self.load_state_dict_pre_hook,
                    self,  # model
                    task_adapt["dst_classes"],  # model_classes
                    task_adapt["src_classes"],  # chkpt_classes
                )
            )

    @staticmethod
    def load_state_dict_pre_hook(model, model_classes, chkpt_classes, chkpt_dict, prefix, *args, **kwargs):
        """Modify input state_dict according to class name matching before weight loading"""
        logger.info(f"----------------- CustomMaskRCNN.load_state_dict_pre_hook() called w/ prefix: {prefix}")

        # Dst to src mapping index
        model_dict = model.state_dict()
        model_classes = list(model_classes)
        chkpt_classes = list(chkpt_classes)
        model2chkpt = map_class_names(model_classes, chkpt_classes)
        logger.info(f"{chkpt_classes} -> {model_classes} ({model2chkpt})")

        # List of class-relevant params & their row-stride
        param_strides = {
            "roi_head.bbox_head.fc_cls.weight": 1,
            "roi_head.bbox_head.fc_cls.bias": 1,
            "roi_head.bbox_head.fc_reg.weight": 4,  # 4 rows (bbox) for each class
            "roi_head.bbox_head.fc_reg.bias": 4,
        }

        for model_name, stride in param_strides.items():
            chkpt_name = prefix + model_name
            if model_name not in model_dict or chkpt_name not in chkpt_dict:
                logger.info(f"Skipping weight copy: {chkpt_name}")
                continue

            # Mix weights
            model_param = model_dict[model_name].clone()
            chkpt_param = chkpt_dict[chkpt_name]
            for m, c in enumerate(model2chkpt):
                if c >= 0:
                    # Copying only matched weight rows
                    model_param[(m) * stride : (m + 1) * stride].copy_(chkpt_param[(c) * stride : (c + 1) * stride])
            if model_param.shape[0] > len(model_classes * stride):  # BG class
                c = len(chkpt_classes)
                m = len(model_classes)
                model_param[(m) * stride : (m + 1) * stride].copy_(chkpt_param[(c) * stride : (c + 1) * stride])

            # Replace checkpoint weight by mixed weights
            chkpt_dict[chkpt_name] = model_param


if is_mmdeploy_enabled():
    from mmdeploy.core import FUNCTION_REWRITER

    from otx.mpa.modules.hooks.recording_forward_hooks import (
        ActivationMapHook,
        FeatureVectorHook,
    )

    @FUNCTION_REWRITER.register_rewriter(
        "otx.mpa.modules.models.detectors.custom_maskrcnn_detector.CustomMaskRCNN.simple_test"
    )
    def custom_mask_rcnn__simple_test(ctx, self, img, img_metas, proposals=None, **kwargs):
        assert self.with_bbox, "Bbox head must be implemented."
        x = self.extract_feat(img)
        feature_vector = FeatureVectorHook.func(x)
        sailency_map = ActivationMapHook.func(x[-1])
        if proposals is None:
            proposals, _ = self.rpn_head.simple_test_rpn(x, img_metas)
        out = self.roi_head.simple_test(x, proposals, img_metas, rescale=False)
        return (*out, feature_vector, sailency_map)
