"""Unit Test for otx.algorithms.detection.adapters.mmdet.task."""

# Copyright (C) 2023 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#

import os
import json
from contextlib import nullcontext
from copy import deepcopy
from typing import Any, Dict

import numpy as np
import pytest
import torch
from mmcv.utils import Config
from torch import nn

from otx.algorithms.common.adapters.mmcv.utils.config_utils import MPAConfig
from otx.algorithms.detection.adapters.mmdet.task import MMDetectionTask
from otx.algorithms.detection.adapters.mmdet.models.detectors.custom_atss_detector import CustomATSS
from otx.algorithms.detection.configs.base import DetectionConfig
from otx.api.configuration import ConfigurableParameters
from otx.api.configuration.helper import create
from otx.api.entities.dataset_item import DatasetItemEntity
from otx.api.entities.datasets import DatasetEntity
from otx.api.entities.explain_parameters import ExplainParameters
from otx.api.entities.inference_parameters import InferenceParameters
from otx.api.entities.label import Domain
from otx.api.entities.label_schema import LabelGroup, LabelGroupType, LabelSchemaEntity
from otx.api.entities.model import (
    ModelConfiguration,
    ModelEntity,
    ModelFormat,
    ModelOptimizationType,
    ModelPrecision,
)
from otx.api.entities.model_template import InstantiationType, parse_model_template, TaskFamily, TaskType
from otx.api.entities.resultset import ResultSetEntity
from otx.api.usecases.tasks.interfaces.export_interface import ExportType
from tests.test_suite.e2e_test_system import e2e_pytest_unit
from tests.unit.algorithms.detection.test_helpers import (
    DEFAULT_DET_TEMPLATE_DIR,
    DEFAULT_ISEG_TEMPLATE_DIR,
    init_environment,
    generate_det_dataset,
)


class MockModule(nn.Module):
    """Mock class for nn.Module."""

    def forward(self, inputs: Any):
        return inputs


class MockModel(nn.Module):
    """Mock class for pytorch model."""

    def __init__(self, task_type):
        super().__init__()
        self.module = MockModule()
        self.module.backbone = MockModule()
        self.backbone = MockModule()
        self.task_type = task_type

    def forward(self, *args, **kwargs):
        forward_hooks = list(self.module.backbone._forward_hooks.values())
        for hook in forward_hooks:
            hook(1, 2, 3)
        return [[np.array([[0, 0, 1, 1, 0.1]]), np.array([[0, 0, 1, 1, 0.2]]), np.array([[0, 0, 1, 1, 0.7]])]]

    @staticmethod
    def named_parameters():
        return {"name": torch.Tensor([0.5])}.items()


class MockDataset(DatasetEntity):
    """Mock class for mm_dataset."""

    def __init__(self, dataset: DatasetEntity, task_type: str):
        self.dataset = dataset
        self.task_type = task_type
        self.CLASSES = ["1", "2", "3"]

    def __len__(self):
        return len(self.dataset)

    def evaluate(self, prediction, *args, **kwargs):
        if self.task_type == "det":
            return {"mAP": 1.0}
        else:
            return {"mAP": 1.0}


class MockDataLoader:
    """Mock class for data loader."""

    def __init__(self, dataset: DatasetEntity):
        self.dataset = dataset
        self.iter = iter(self.dataset)

    def __len__(self) -> int:
        return len(self.dataset)

    def __next__(self) -> Dict[str, DatasetItemEntity]:
        return {"imgs": next(self.iter)}

    def __iter__(self):
        return self


class MockExporter:
    """Mock class for Exporter."""

    def __init__(self, task):
        self._output_path = task._output_path

    def run(self, *args, **kwargs):
        with open(os.path.join(self._output_path, "openvino.bin"), "wb") as f:
            f.write(np.ndarray([0]))
        with open(os.path.join(self._output_path, "openvino.xml"), "wb") as f:
            f.write(np.ndarray([0]))
        with open(os.path.join(self._output_path, "model.onnx"), "wb") as f:
            f.write(np.ndarray([0]))

        return {
            "outputs": {
                "bin": os.path.join(self._output_path, "openvino.bin"),
                "xml": os.path.join(self._output_path, "openvino.xml"),
                "onnx": os.path.join(self._output_path, "model.onnx"),
            }
        }


class TestMMActionTask:
    """Test class for MMActionTask.

    Details are explained in each test function.
    """

    @pytest.fixture(autouse=True)
    def setup(self) -> None:
        model_template = parse_model_template(os.path.join(DEFAULT_DET_TEMPLATE_DIR, "template.yaml"))
        hyper_parameters = create(model_template.hyper_parameters.data)
        task_env = init_environment(hyper_parameters, model_template, task_type=TaskType.DETECTION)

        self.det_task = MMDetectionTask(task_env)

        self.det_dataset, self.det_labels = generate_det_dataset(TaskType.DETECTION, 100)
        self.det_label_schema = LabelSchemaEntity()
        det_label_group = LabelGroup(
            name="labels",
            labels=self.det_labels,
            group_type=LabelGroupType.EXCLUSIVE,
        )
        self.det_label_schema.add_group(det_label_group)

        model_template = parse_model_template(os.path.join(DEFAULT_ISEG_TEMPLATE_DIR, "template.yaml"))
        hyper_parameters = create(model_template.hyper_parameters.data)
        task_env = init_environment(hyper_parameters, model_template, task_type=TaskType.INSTANCE_SEGMENTATION)

        self.iseg_task = MMDetectionTask(task_env)

        self.iseg_dataset, self.iseg_labels = generate_det_dataset(TaskType.INSTANCE_SEGMENTATION, 100)
        self.iseg_label_schema = LabelSchemaEntity()
        iseg_label_group = LabelGroup(
            name="labels",
            labels=self.iseg_labels,
            group_type=LabelGroupType.EXCLUSIVE,
        )
        self.iseg_label_schema.add_group(iseg_label_group)

    @e2e_pytest_unit
    def test_build_model(self, mocker) -> None:
        """Test build_model function."""
        _mock_recipe_cfg = MPAConfig.fromfile(os.path.join(DEFAULT_DET_TEMPLATE_DIR, "model.py"))
        model = self.det_task.build_model(_mock_recipe_cfg, True)
        assert isinstance(model, CustomATSS)

    @e2e_pytest_unit
    def test_train(self, mocker) -> None:
        """Test train function."""

        def _mock_train_detector_det(*args, **kwargs):
            with open(os.path.join(self.det_task._output_path, "latest.pth"), "wb") as f:
                torch.save({"dummy": torch.randn(1, 3, 3, 3)}, f)

        def _mock_train_detector_iseg(*args, **kwargs):
            with open(os.path.join(self.iseg_task._output_path, "latest.pth"), "wb") as f:
                torch.save({"dummy": torch.randn(1, 3, 3, 3)}, f)

        mocker.patch(
            "otx.algorithms.detection.adapters.mmdet.task.build_dataset",
            return_value=MockDataset(self.det_dataset, "det"),
        )
        mocker.patch(
            "otx.algorithms.detection.adapters.mmdet.task.build_dataloader",
            return_value=MockDataLoader(self.det_dataset),
        )
        mocker.patch(
            "otx.algorithms.detection.adapters.mmdet.task.patch_data_pipeline",
            return_value=True,
        )
        mocker.patch(
            "otx.algorithms.detection.adapters.mmdet.task.train_detector",
            side_effect=_mock_train_detector_det,
        )
        mocker.patch(
            "otx.algorithms.detection.adapters.mmdet.task.single_gpu_test",
            return_value=[
                np.array([np.array([[0, 0, 1, 1, 0.1]]), np.array([[0, 0, 1, 1, 0.2]]), np.array([[0, 0, 1, 1, 0.7]])])
            ]
            * 100,
        )
        mocker.patch(
            "otx.algorithms.detection.adapters.mmdet.task.FeatureVectorHook",
            return_value=nullcontext(),
        )

        _config = ModelConfiguration(DetectionConfig(), self.det_label_schema)
        output_model = ModelEntity(self.det_dataset, _config)
        self.det_task.train(self.det_dataset, output_model)
        output_model.performance == 1.0

        mocker.patch(
            "otx.algorithms.detection.adapters.mmdet.task.train_detector",
            side_effect=_mock_train_detector_iseg,
        )
        mocker.patch(
            "otx.algorithms.detection.adapters.mmdet.task.single_gpu_test",
            return_value=[(np.array([[[0, 0, 1, 1, 1]]]), np.array([[[0, 0, 1, 1, 1, 1, 1]]]))] * 100,
        )
        _config = ModelConfiguration(DetectionConfig(), self.iseg_label_schema)
        output_model = ModelEntity(self.iseg_dataset, _config)
        self.iseg_task.train(self.iseg_dataset, output_model)
        output_model.performance == 1.0

    @e2e_pytest_unit
    def test_infer(self, mocker) -> None:
        """Test infer function."""

        mocker.patch(
            "otx.algorithms.detection.adapters.mmdet.task.build_dataset",
            return_value=MockDataset(self.det_dataset, "det"),
        )
        mocker.patch(
            "otx.algorithms.detection.adapters.mmdet.task.build_dataloader",
            return_value=MockDataLoader(self.det_dataset),
        )
        mocker.patch(
            "otx.algorithms.detection.adapters.mmdet.task.patch_data_pipeline",
            return_value=True,
        )
        mocker.patch(
            "otx.algorithms.detection.adapters.mmdet.task.single_gpu_test",
            return_value=[
                np.array([np.array([[0, 0, 1, 1, 0.1]]), np.array([[0, 0, 1, 1, 0.2]]), np.array([[0, 0, 1, 1, 0.7]])])
            ]
            * 100,
        )
        mocker.patch(
            "otx.algorithms.detection.adapters.mmdet.task.FeatureVectorHook",
            return_value=nullcontext(),
        )

        inference_parameters = InferenceParameters(is_evaluation=True)
        outputs = self.det_task.infer(self.det_dataset, inference_parameters)
        for output in outputs:
            assert output.get_annotations()[-1].get_labels()[0].probability == 0.7

    @e2e_pytest_unit
    def test_det_evaluate(self) -> None:
        """Test evaluate function for detection."""

        _config = ModelConfiguration(DetectionConfig(), self.det_label_schema)
        _model = ModelEntity(self.det_dataset, _config)
        resultset = ResultSetEntity(_model, self.det_dataset, self.det_dataset)
        self.det_task.evaluate(resultset)
        assert resultset.performance.score.value == 1.0

    @e2e_pytest_unit
    def test_det_evaluate_with_empty_annotations(self) -> None:
        """Test evaluate function for detection with empty predictions."""

        _config = ModelConfiguration(DetectionConfig(), self.det_label_schema)
        _model = ModelEntity(self.det_dataset, _config)
        resultset = ResultSetEntity(_model, self.det_dataset, self.det_dataset.with_empty_annotations())
        self.det_task.evaluate(resultset)
        assert resultset.performance.score.value == 0.0

    @e2e_pytest_unit
    def test_iseg_evaluate(self) -> None:
        """Test evaluate function for instance segmentation."""

        _config = ModelConfiguration(DetectionConfig(), self.iseg_label_schema)
        _model = ModelEntity(self.iseg_dataset, _config)
        resultset = ResultSetEntity(_model, self.iseg_dataset, self.iseg_dataset)
        self.iseg_task.evaluate(resultset)
        assert resultset.performance.score.value == 1.0

    @pytest.mark.parametrize("precision", [ModelPrecision.FP16, ModelPrecision.FP32])
    @e2e_pytest_unit
    def test_export(self, mocker, precision: ModelPrecision) -> None:
        """Test export function.

        <Steps>
            1. Create model entity
            2. Run export function
            3. Check output model attributes
        """
        _config = ModelConfiguration(DetectionConfig(), self.det_label_schema)
        _model = ModelEntity(self.det_dataset, _config)

        mocker.patch(
            "otx.algorithms.detection.adapters.mmdet.task.DetectionExporter",
            return_value=MockExporter(self.det_task),
        )
        mocker.patch(
            "otx.algorithms.detection.task.embed_ir_model_data",
            return_value=True,
        )

        self.det_task.export(ExportType.OPENVINO, _model, precision, False)

        assert _model.model_format == ModelFormat.OPENVINO
        assert _model.optimization_type == ModelOptimizationType.MO
        assert _model.precision[0] == precision
        assert _model.get_data("openvino.bin") is not None
        assert _model.get_data("openvino.xml") is not None
        assert _model.get_data("confidence_threshold") is not None
        assert _model.precision == self.det_task._precision
        assert _model.optimization_methods == self.det_task._optimization_methods
        assert _model.get_data("label_schema.json") is not None

    @e2e_pytest_unit
    def test_explain(self, mocker):
        """Test explain function."""

        mocker.patch(
            "otx.algorithms.detection.adapters.mmdet.task.build_dataset",
            return_value=MockDataset(self.det_dataset, "det"),
        )
        mocker.patch(
            "otx.algorithms.detection.adapters.mmdet.task.build_dataloader",
            return_value=MockDataLoader(self.det_dataset),
        )
        mocker.patch(
            "otx.algorithms.detection.adapters.mmdet.task.patch_data_pipeline",
            return_value=True,
        )
        mocker.patch(
            "otx.algorithms.detection.adapters.mmdet.task.build_data_parallel",
            return_value=MockModel(TaskType.DETECTION),
        )

        explain_parameters = ExplainParameters(
            explainer="ClassWiseSaliencyMap",
            process_saliency_maps=False,
            explain_predicted_classes=True,
        )
        outputs = self.det_task.explain(self.det_dataset, explain_parameters)
