"""Openvino Task of Detection."""

# Copyright (C) 2021 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions
# and limitations under the License.

import copy
import io
import json
import multiprocessing
import os
import tempfile
import time
import warnings
from typing import Any, Dict, List, Optional, Tuple, Union
from zipfile import ZipFile

import attr
import numpy as np
from addict import Dict as ADDict
from compression.api import DataLoader
from compression.engines.ie_engine import IEEngine
from compression.graph import load_model, save_model
from compression.graph.model_utils import compress_model_weights, get_nodes_by_type
from compression.pipeline.initializer import create_pipeline
from openvino.model_zoo.model_api.adapters import OpenvinoAdapter, create_core
from openvino.model_zoo.model_api.models import Model

from otx.algorithms.common.utils.logger import get_logger
from otx.algorithms.detection.adapters.openvino import model_wrappers
from otx.algorithms.detection.configs.base import DetectionConfig
from otx.api.configuration.helper.utils import (
    config_to_bytes,
    flatten_config_values,
    flatten_detection_config_groups,
    merge_a_into_b,
)
from otx.api.entities.annotation import AnnotationSceneEntity
from otx.api.entities.datasets import DatasetEntity
from otx.api.entities.explain_parameters import ExplainParameters
from otx.api.entities.inference_parameters import (
    InferenceParameters,
    default_progress_callback,
)
from otx.api.entities.label import Domain, LabelEntity
from otx.api.entities.label_schema import LabelSchemaEntity
from otx.api.entities.model import (
    ModelEntity,
    ModelFormat,
    ModelOptimizationType,
    ModelPrecision,
    OptimizationMethod,
)
from otx.api.entities.model_template import TaskType
from otx.api.entities.optimization_parameters import OptimizationParameters
from otx.api.entities.resultset import ResultSetEntity
from otx.api.entities.subset import Subset
from otx.api.entities.task_environment import TaskEnvironment
from otx.api.entities.tensor import TensorEntity
from otx.api.serialization.label_mapper import LabelSchemaMapper, label_schema_to_bytes
from otx.api.usecases.evaluation.metrics_helper import MetricsHelper
from otx.api.usecases.exportable_code import demo
from otx.api.usecases.exportable_code.inference import BaseInferencer
from otx.api.usecases.exportable_code.prediction_to_annotation_converter import (
    DetectionToAnnotationConverter,
    IPredictionToAnnotationConverter,
    MaskToAnnotationConverter,
    RotatedRectToAnnotationConverter,
)
from otx.api.usecases.tasks.interfaces.deployment_interface import IDeploymentTask
from otx.api.usecases.tasks.interfaces.evaluate_interface import IEvaluationTask
from otx.api.usecases.tasks.interfaces.inference_interface import IInferenceTask
from otx.api.usecases.tasks.interfaces.optimization_interface import (
    IOptimizationTask,
    OptimizationType,
)
from otx.api.utils.dataset_utils import add_saliency_maps_to_dataset_item
from otx.api.utils.detection_utils import detection2array
from otx.api.utils.tiler import Tiler

logger = get_logger()


# pylint: disable=too-many-locals
class BaseInferencerWithConverter(BaseInferencer):
    """BaseInferencerWithConverter class in OpenVINO task."""

    def __init__(
        self,
        configuration: dict,
        model: Model,
        converter: IPredictionToAnnotationConverter,
    ) -> None:
        self.configuration = configuration
        self.model = model
        self.converter = converter

    def pre_process(self, image: np.ndarray) -> Tuple[Dict[str, np.ndarray], Dict[str, Any]]:
        """Pre-process function of OpenVINO Detection Inferencer."""
        return self.model.preprocess(image)

    def post_process(self, prediction: Dict[str, np.ndarray], metadata: Dict[str, Any]) -> AnnotationSceneEntity:
        """Post-process function of OpenVINO Detection Inferencer."""
        detections = self.model.postprocess(prediction, metadata)

        return self.converter.convert_to_annotation(detections, metadata)

    def predict(self, image: np.ndarray):
        """Predict function of OpenVINO Detection Inferencer."""
        image, metadata = self.pre_process(image)
        raw_predictions = self.forward(image)
        predictions = self.post_process(raw_predictions, metadata)
        if "feature_vector" not in raw_predictions or "saliency_map" not in raw_predictions:
            warnings.warn(
                "Could not find Feature Vector and Saliency Map in OpenVINO output. "
                "Please rerun OpenVINO export or retrain the model."
            )
            features = (None, None)
        else:
            features = (
                raw_predictions["feature_vector"].reshape(-1),
                raw_predictions["saliency_map"][0],
            )
        return predictions, features

    def forward(self, image: Dict[str, np.ndarray]) -> Dict[str, np.ndarray]:
        """Forward function of OpenVINO Detection Inferencer."""
        return self.model.infer_sync(image)


class OpenVINODetectionInferencer(BaseInferencerWithConverter):
    """Inferencer implementation for OTXDetection using OpenVINO backend."""

    def __init__(
        self,
        hparams: DetectionConfig,
        label_schema: LabelSchemaEntity,
        model_file: Union[str, bytes],
        weight_file: Union[str, bytes, None] = None,
        device: str = "CPU",
        num_requests: int = 1,
    ):
        """Initialize for OpenVINODetectionInferencer.

        :param hparams: Hyper parameters that the model should use.
        :param label_schema: LabelSchemaEntity that was used during model training.
        :param model_file: Path OpenVINO IR model definition file.
        :param weight_file: Path OpenVINO IR model weights file.
        :param device: Device to run inference on, such as CPU, GPU or MYRIAD. Defaults to "CPU".
        :param num_requests: Maximum number of requests that the inferencer can make. Defaults to 1.
        """

        model_adapter = OpenvinoAdapter(
            create_core(),
            model_file,
            weight_file,
            device=device,
            max_num_requests=num_requests,
        )
        configuration = {
            **attr.asdict(
                hparams.postprocessing,
                filter=lambda attr, value: attr.name not in ["header", "description", "type", "visible_in_ui"],
            )
        }
        model = Model.create_model("OTX_SSD", model_adapter, configuration, preload=True)
        converter = DetectionToAnnotationConverter(label_schema)

        super().__init__(configuration, model, converter)

    def post_process(self, prediction: Dict[str, np.ndarray], metadata: Dict[str, Any]) -> AnnotationSceneEntity:
        """Detection specific post-process."""
        detections = self.model.postprocess(prediction, metadata)
        detections = detection2array(detections)
        return self.converter.convert_to_annotation(detections, metadata)


class OpenVINOMaskInferencer(BaseInferencerWithConverter):
    """Mask Inferencer implementation for OTXDetection using OpenVINO backend."""

    def __init__(
        self,
        hparams: DetectionConfig,
        label_schema: LabelSchemaEntity,
        model_file: Union[str, bytes],
        weight_file: Union[str, bytes, None] = None,
        device: str = "CPU",
        num_requests: int = 1,
    ):
        model_adapter = OpenvinoAdapter(
            create_core(),
            model_file,
            weight_file,
            device=device,
            max_num_requests=num_requests,
        )

        configuration = {
            **attr.asdict(
                hparams.postprocessing,
                filter=lambda attr, value: attr.name not in ["header", "description", "type", "visible_in_ui"],
            )
        }

        model = Model.create_model("OTX_MaskRCNN", model_adapter, configuration, preload=True)

        converter = MaskToAnnotationConverter(label_schema)

        super().__init__(configuration, model, converter)


class OpenVINORotatedRectInferencer(BaseInferencerWithConverter):
    """Rotated Rect Inferencer implementation for OTXDetection using OpenVINO backend."""

    def __init__(
        self,
        hparams: DetectionConfig,
        label_schema: LabelSchemaEntity,
        model_file: Union[str, bytes],
        weight_file: Union[str, bytes, None] = None,
        device: str = "CPU",
        num_requests: int = 1,
    ):
        model_adapter = OpenvinoAdapter(
            create_core(),
            model_file,
            weight_file,
            device=device,
            max_num_requests=num_requests,
        )

        configuration = {
            **attr.asdict(
                hparams.postprocessing,
                filter=lambda attr, value: attr.name not in ["header", "description", "type", "visible_in_ui"],
            )
        }

        model = Model.create_model("OTX_MaskRCNN", model_adapter, configuration, preload=True)

        converter = RotatedRectToAnnotationConverter(label_schema)

        super().__init__(configuration, model, converter)


class OpenVINOTileClassifierWrapper(BaseInferencerWithConverter):
    """Wrapper for OpenVINO Tiling.

    Args:
        inferencer (BaseInferencerWithConverter): inferencer to wrap
        tile_size (int): tile size
        overlap (float): overlap ratio between tiles
        max_number (int): maximum number of objects per image
        tile_classifier_model_file (Union[str, bytes, None], optional): tile classifier xml. Defaults to None.
        tile_classifier_weight_file (Union[str, bytes, None], optional): til classifier weight bin. Defaults to None.
        device (str, optional): device to run inference on, such as CPU, GPU or MYRIAD. Defaults to "CPU".
        num_requests (int, optional): number of request for OpenVINO adapter. Defaults to 1.
        mode (str, optional): run inference in sync or async mode. Defaults to "async".
    """

    def __init__(
        self,
        inferencer: BaseInferencerWithConverter,
        tile_size: int = 400,
        overlap: float = 0.5,
        max_number: int = 100,
        tile_classifier_model_file: Union[str, bytes, None] = None,
        tile_classifier_weight_file: Union[str, bytes, None] = None,
        device: str = "CPU",
        num_requests: int = 1,
        mode: str = "async",
    ):  # pylint: disable=too-many-arguments
        assert mode in ["async", "sync"], "mode should be async or sync"
        classifier = None
        if tile_classifier_model_file is not None or tile_classifier_weight_file is not None:
            adapter = OpenvinoAdapter(
                create_core(),
                tile_classifier_model_file,
                tile_classifier_weight_file,
                device=device,
                max_num_requests=num_requests,
            )
            classifier = Model(model_adapter=adapter, preload=True)

        self.tiler = Tiler(
            tile_size=tile_size,
            overlap=overlap,
            max_number=max_number,
            detector=inferencer.model,
            classifier=classifier,
            mode=mode,
            segm=bool(isinstance(inferencer.converter, (MaskToAnnotationConverter, RotatedRectToAnnotationConverter))),
        )

        super().__init__(inferencer.configuration, inferencer.model, inferencer.converter)

    def predict(
        self, image: np.ndarray, mode: str = "async"
    ) -> Tuple[AnnotationSceneEntity, Tuple[np.ndarray, np.ndarray]]:
        """Run prediction by tiling image to small patches.

        Args:
            image (np.ndarray): input image
            mode (str, optional): run inference in sync or async mode. Defaults to 'async'.

        Returns:
            detections: AnnotationSceneEntity
            features: list including saliency map and feature vector
        """
        detections, features = self.tiler.predict(image, mode)
        detections = self.converter.convert_to_annotation(detections, metadata={"original_shape": image.shape})
        return detections, features


class OTXOpenVinoDataLoader(DataLoader):
    """Data loader for OTXDetection using OpenVINO backend."""

    def __init__(self, dataset: DatasetEntity, inferencer: BaseInferencer):
        self.dataset = dataset
        self.inferencer = inferencer

    def __getitem__(self, index: int):
        """Return dataset item from index."""
        image = self.dataset[index].numpy
        annotation = self.dataset[index].annotation_scene
        inputs, metadata = self.inferencer.pre_process(image)

        return (index, annotation), inputs, metadata

    def __len__(self):
        """Length of OTXOpenVinoDataLoader."""
        return len(self.dataset)


class OpenVINODetectionTask(IDeploymentTask, IInferenceTask, IEvaluationTask, IOptimizationTask):
    """Task implementation for OTXDetection using OpenVINO backend."""

    def __init__(self, task_environment: TaskEnvironment):
        logger.info("Loading OpenVINO OTXDetectionTask")
        self.task_environment = task_environment
        self.model = self.task_environment.model
        self.task_type = self.task_environment.model_template.task_type
        self.confidence_threshold: float = 0.0
        self.config = self.load_config()
        self.inferencer = self.load_inferencer()
        logger.info("OpenVINO task initialization completed")

    @property
    def hparams(self):
        """Hparams of OpenVINO Detection Task."""
        return self.task_environment.get_hyper_parameters(DetectionConfig)

    def load_config(self) -> ADDict:
        """Load configurable parameters from model adapter.

        Returns:
            ADDict: config dictionary
        """
        config = vars(self.hparams)
        flatten_detection_config_groups(config)
        try:
            if self.model is not None and self.model.get_data("config.json"):
                json_dict = json.loads(self.model.get_data("config.json"))
                flatten_config_values(json_dict)
                config = merge_a_into_b(json_dict, config)
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(f"Failed to load config.json: {e}")
        config = ADDict(config)
        return config

    def load_inferencer(
        self,
    ) -> Union[
        OpenVINODetectionInferencer,
        OpenVINOMaskInferencer,
        OpenVINORotatedRectInferencer,
        OpenVINOTileClassifierWrapper,
    ]:
        """load_inferencer function of OpenVINO Detection Task."""
        if self.model is None:
            raise RuntimeError("load_inferencer failed, model is None")
        _hparams = copy.deepcopy(self.hparams)
        self.confidence_threshold = float(
            np.frombuffer(self.model.get_data("confidence_threshold"), dtype=np.float32)[0]
        )
        _hparams.postprocessing.confidence_threshold = self.confidence_threshold
        args = [
            _hparams,
            self.task_environment.label_schema,
            self.model.get_data("openvino.xml"),
            self.model.get_data("openvino.bin"),
        ]
        if self.task_type == TaskType.DETECTION:
            inferencer: BaseInferencerWithConverter = OpenVINODetectionInferencer(*args)
        if self.task_type == TaskType.INSTANCE_SEGMENTATION:
            inferencer = OpenVINOMaskInferencer(*args)
        if self.task_type == TaskType.ROTATED_DETECTION:
            inferencer = OpenVINORotatedRectInferencer(*args)
        if self.config.tiling_parameters.enable_tiling:
            logger.info("Tiling is enabled. Wrap inferencer with tile inference.")
            tile_classifier_model_file, tile_classifier_weight_file = None, None
            if self.config.tiling_parameters.enable_tile_classifier:
                logger.info("Tile classifier is enabled. Load tile classifier model.")
                tile_classifier_model_file = self.model.get_data("tile_classifier.xml")
                tile_classifier_weight_file = self.model.get_data("tile_classifier.bin")
            inferencer = OpenVINOTileClassifierWrapper(
                inferencer,
                self.config.tiling_parameters.tile_size,
                self.config.tiling_parameters.tile_overlap,
                self.config.tiling_parameters.tile_max_number,
                tile_classifier_model_file,
                tile_classifier_weight_file,
            )
        if not isinstance(
            inferencer,
            (
                OpenVINODetectionInferencer,
                OpenVINOMaskInferencer,
                OpenVINORotatedRectInferencer,
                OpenVINOTileClassifierWrapper,
            ),
        ):
            raise RuntimeError(f"Unknown OpenVINO Inferencer TaskType: {self.task_type}")
        return inferencer

    def infer(
        self,
        dataset: DatasetEntity,
        inference_parameters: Optional[InferenceParameters] = None,
    ) -> DatasetEntity:
        """Infer function of OpenVINODetectionTask."""
        logger.info("Start OpenVINO inference")

        if inference_parameters is not None:
            update_progress_callback = inference_parameters.update_progress
            add_saliency_map = not inference_parameters.is_evaluation
            process_saliency_maps = inference_parameters.process_saliency_maps
            explain_predicted_classes = inference_parameters.explain_predicted_classes
        else:
            update_progress_callback = default_progress_callback
            add_saliency_map = True
            process_saliency_maps = False
            explain_predicted_classes = True

        total_time = 0.0
        dataset_size = len(dataset)
        for i, dataset_item in enumerate(dataset, 1):
            start_time = time.perf_counter()
            predicted_scene, features = self.inferencer.predict(dataset_item.numpy)
            dataset_item.append_annotations(predicted_scene.annotations)
            feature_vector, saliency_map = features
            if feature_vector is not None:
                representation_vector = TensorEntity(name="representation_vector", numpy=feature_vector.reshape(-1))
                dataset_item.append_metadata_item(representation_vector, model=self.model)

            if add_saliency_map and saliency_map is not None:
                labels = self.task_environment.get_labels().copy()
                if saliency_map.shape[0] == len(labels) + 1:
                    # Include the background as the last category
                    labels.append(LabelEntity("background", Domain.DETECTION))

                predicted_scored_labels: List = []
                for bbox in predicted_scene.annotations:
                    predicted_scored_labels += bbox.get_labels()

                add_saliency_maps_to_dataset_item(
                    dataset_item=dataset_item,
                    saliency_map=saliency_map,
                    model=self.model,
                    labels=labels,
                    predicted_scored_labels=predicted_scored_labels,
                    explain_predicted_classes=explain_predicted_classes,
                    process_saliency_maps=process_saliency_maps,
                )
            update_progress_callback(int(i / dataset_size * 100), None)
            end_time = time.perf_counter() - start_time
            logger.info(f"{end_time} secs")
            total_time += end_time
        logger.info(f"Avg time per image: {total_time/len(dataset)} secs")
        logger.info(f"Total time: {total_time} secs")
        logger.info("OpenVINO inference completed")
        return dataset

    def explain(
        self,
        dataset: DatasetEntity,
        explain_parameters: Optional[ExplainParameters] = None,
    ) -> DatasetEntity:
        """Explain function of OpenVINODetectionTask."""
        logger.info("Start OpenVINO explain")

        update_progress_callback = default_progress_callback
        process_saliency_maps = False
        explain_predicted_classes = True
        if explain_parameters is not None:
            update_progress_callback = explain_parameters.update_progress  # type: ignore
            process_saliency_maps = explain_parameters.process_saliency_maps
            explain_predicted_classes = explain_parameters.explain_predicted_classes

        dataset_size = len(dataset)
        for i, dataset_item in enumerate(dataset, 1):
            predicted_scene, features = self.inferencer.predict(dataset_item.numpy)
            dataset_item.append_annotations(predicted_scene.annotations)
            update_progress_callback(int(i / dataset_size * 100), None)
            _, saliency_map = features
            if saliency_map is None:
                raise RuntimeError(
                    "There is no Saliency Map in OpenVINO IR model output. "
                    "Please export model to OpenVINO IR with dump_features"
                )

            labels = self.task_environment.get_labels().copy()
            if saliency_map.shape[0] == len(labels) + 1:
                # Include the background as the last category
                labels.append(LabelEntity("background", Domain.DETECTION))

            predicted_scored_labels: List = []
            for bbox in predicted_scene.annotations:
                predicted_scored_labels += bbox.get_labels()

            add_saliency_maps_to_dataset_item(
                dataset_item=dataset_item,
                saliency_map=saliency_map,
                model=self.model,
                labels=labels,
                predicted_scored_labels=predicted_scored_labels,
                explain_predicted_classes=explain_predicted_classes,
                process_saliency_maps=process_saliency_maps,
            )
        logger.info("OpenVINO explain completed")
        return dataset

    def evaluate(
        self,
        output_resultset: ResultSetEntity,
        evaluation_metric: Optional[str] = None,
    ):
        """Evaluate function of OpenVINODetectionTask."""
        logger.info("Start OpenVINO metric evaluation")
        if evaluation_metric is not None:
            logger.warning(
                f"Requested to use {evaluation_metric} metric, but parameter is ignored. Use F-measure instead."
            )
        output_resultset.performance = MetricsHelper.compute_f_measure(output_resultset).get_performance()
        logger.info("OpenVINO metric evaluation completed")

    def deploy(self, output_model: ModelEntity) -> None:
        """Deploy function of OpenVINODetectionTask."""
        logger.info("Deploying the model")

        work_dir = os.path.dirname(demo.__file__)
        parameters = {}
        parameters["type_of_model"] = self.inferencer.model.__model__
        parameters["converter_type"] = str(self.task_type)
        parameters["model_parameters"] = self.inferencer.configuration
        parameters["model_parameters"]["labels"] = LabelSchemaMapper.forward(self.task_environment.label_schema)
        if self.config.tiling_parameters.get("type"):
            self.config.tiling_parameters["type"] = str(self.config.tiling_parameters["type"])
        parameters["tiling_parameters"] = self.config.tiling_parameters

        zip_buffer = io.BytesIO()
        with ZipFile(zip_buffer, "w") as arch:
            # model files
            if self.model is None:
                raise ValueError("Deploy failed, model is None")
            arch.writestr(os.path.join("model", "model.xml"), self.model.get_data("openvino.xml"))
            arch.writestr(os.path.join("model", "model.bin"), self.model.get_data("openvino.bin"))
            if self.config.tiling_parameters.enable_tiling and self.config.tiling_parameters.enable_tile_classifier:
                arch.writestr(os.path.join("model", "tile_classifier.xml"), self.model.get_data("tile_classifier.xml"))
                arch.writestr(os.path.join("model", "tile_classifier.bin"), self.model.get_data("tile_classifier.bin"))
            arch.writestr(
                os.path.join("model", "config.json"),
                json.dumps(parameters, ensure_ascii=False, indent=4),
            )
            # model_wrappers files
            for root, _, files in os.walk(os.path.dirname(model_wrappers.__file__)):
                for file in files:
                    file_path = os.path.join(root, file)
                    arch.write(
                        file_path,
                        os.path.join(
                            "python",
                            "model_wrappers",
                            file_path.split("model_wrappers/")[1],
                        ),
                    )
            # python files
            arch.write(
                os.path.join(work_dir, "requirements.txt"),
                os.path.join("python", "requirements.txt"),
            )
            arch.write(os.path.join(work_dir, "LICENSE"), os.path.join("python", "LICENSE"))
            arch.write(os.path.join(work_dir, "README.md"), os.path.join("python", "README.md"))
            arch.write(os.path.join(work_dir, "demo.py"), os.path.join("python", "demo.py"))
        output_model.exportable_code = zip_buffer.getvalue()
        logger.info("Deploying completed")

    def optimize(
        self,
        optimization_type: OptimizationType,
        dataset: DatasetEntity,
        output_model: ModelEntity,
        optimization_parameters: Optional[OptimizationParameters] = None,
    ):
        """Optimize function of OpenVINODetectionTask."""
        logger.info("Start POT optimization")

        if optimization_type is not OptimizationType.POT:
            raise ValueError("POT is the only supported optimization type for OpenVino models")
        if self.model is None:
            raise RuntimeError("Optimize failed, model is None")

        dataset = dataset.get_subset(Subset.TRAINING)
        data_loader = OTXOpenVinoDataLoader(dataset, self.inferencer)

        with tempfile.TemporaryDirectory() as tempdir:
            xml_path = os.path.join(tempdir, "model.xml")
            bin_path = os.path.join(tempdir, "model.bin")
            with open(xml_path, "wb") as f:
                f.write(self.model.get_data("openvino.xml"))
            with open(bin_path, "wb") as f:
                f.write(self.model.get_data("openvino.bin"))

            model_config = ADDict({"model_name": "openvino_model", "model": xml_path, "weights": bin_path})

            model = load_model(model_config)

            if get_nodes_by_type(model, ["FakeQuantize"]):
                raise RuntimeError("Model is already optimized by POT")

        if optimization_parameters:
            optimization_parameters.update_progress(10, None)

        engine_config = ADDict(
            {
                "device": "CPU",
                "stat_requests_number": min(
                    self.hparams.pot_parameters.stat_requests_number, multiprocessing.cpu_count()
                ),
            }
        )

        stat_subset_size = self.hparams.pot_parameters.stat_subset_size
        preset = self.hparams.pot_parameters.preset.name.lower()

        algorithms = [
            {
                "name": "DefaultQuantization",
                "params": {
                    "target_device": "ANY",
                    "preset": preset,
                    "stat_subset_size": min(stat_subset_size, len(data_loader)),
                    "shuffle_data": True,
                },
            }
        ]

        engine = IEEngine(config=engine_config, data_loader=data_loader, metric=None)

        pipeline = create_pipeline(algorithms, engine)

        compressed_model = pipeline.run(model)

        compress_model_weights(compressed_model)

        if optimization_parameters:
            optimization_parameters.update_progress(90, None)

        with tempfile.TemporaryDirectory() as tempdir:
            save_model(compressed_model, tempdir, model_name="model")
            with open(os.path.join(tempdir, "model.xml"), "rb") as f:
                output_model.set_data("openvino.xml", f.read())
            with open(os.path.join(tempdir, "model.bin"), "rb") as f:
                output_model.set_data("openvino.bin", f.read())
            output_model.set_data(
                "confidence_threshold",
                np.array([self.confidence_threshold], dtype=np.float32).tobytes(),
            )

        output_model.set_data(
            "label_schema.json",
            label_schema_to_bytes(self.task_environment.label_schema),
        )
        output_model.set_data("config.json", config_to_bytes(self.hparams))

        # set model attributes for quantized model
        output_model.model_format = ModelFormat.OPENVINO
        output_model.optimization_type = ModelOptimizationType.POT
        output_model.optimization_methods = [OptimizationMethod.QUANTIZATION]
        output_model.precision = [ModelPrecision.INT8]

        self.model = output_model
        self.inferencer = self.load_inferencer()
        logger.info("POT optimization completed")

        if optimization_parameters:
            optimization_parameters.update_progress(100, None)
