# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You
# may not use this file except in compliance with the License. A copy of
# the License is located at
#
#     http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, either express or implied. See the License for the specific
# language governing permissions and limitations under the License.
"""Placeholder docstring"""
from __future__ import print_function, absolute_import

import abc
import datetime
from typing import Any, Dict, Optional, Tuple, Union
import logging

from sagemaker.enums import EndpointType
from sagemaker.deprecations import (
    deprecated_class,
    deprecated_deserialize,
    deprecated_serialize,
    removed_kwargs,
    renamed_kwargs,
    renamed_warning,
)
from sagemaker.deserializers import (  # noqa: F401 # pylint: disable=unused-import
    BytesDeserializer,
    CSVDeserializer,
    JSONDeserializer,
    NumpyDeserializer,
    StreamDeserializer,
    StringDeserializer,
)
from sagemaker.jumpstart.payload_utils import PayloadSerializer
from sagemaker.jumpstart.types import JumpStartSerializablePayload
from sagemaker.jumpstart.utils import get_jumpstart_content_bucket
from sagemaker.model_monitor import (
    DataCaptureConfig,
    DefaultModelMonitor,
    ModelBiasMonitor,
    ModelExplainabilityMonitor,
    ModelMonitor,
    ModelQualityMonitor,
)
from sagemaker.serializers import (
    CSVSerializer,
    IdentitySerializer,
    JSONSerializer,
    NumpySerializer,
)
from sagemaker.session import production_variant, Session
from sagemaker.utils import name_from_base, stringify_object

from sagemaker.model_monitor.model_monitoring import DEFAULT_REPOSITORY_NAME

from sagemaker.lineage.context import EndpointContext
from sagemaker.compute_resource_requirements.resource_requirements import ResourceRequirements

LOGGER = logging.getLogger("sagemaker")


class PredictorBase(abc.ABC):
    """An object that encapsulates a deployed model."""

    @abc.abstractmethod
    def predict(self, *args, **kwargs) -> Any:
        """Perform inference on the provided data and return a prediction."""

    @abc.abstractmethod
    def delete_predictor(self, *args, **kwargs) -> None:
        """Destroy resources associated with this predictor."""

    @property
    @abc.abstractmethod
    def content_type(self) -> str:
        """The MIME type of the data sent to the inference server."""

    @property
    @abc.abstractmethod
    def accept(self) -> Tuple[str]:
        """The content type(s) that are expected from the inference server."""

    def __str__(self) -> str:
        """Overriding str(*) method to make more human-readable."""
        return stringify_object(self)


class Predictor(PredictorBase):
    """Make prediction requests to an Amazon SageMaker endpoint."""

    def __init__(
        self,
        endpoint_name,
        sagemaker_session=None,
        serializer=IdentitySerializer(),
        deserializer=BytesDeserializer(),
        component_name=None,
        **kwargs,
    ):
        """Initialize a ``Predictor``.

        Behavior for serialization of input data and deserialization of
        result data can be configured through initializer arguments. If not
        specified, a sequence of bytes is expected and the API sends it in the
        request body without modifications. In response, the API returns the
        sequence of bytes from the prediction result without any modifications.

        Args:
            endpoint_name (str): Name of the Amazon SageMaker endpoint to which
                requests are sent.
            sagemaker_session (sagemaker.session.Session): A SageMaker Session
                object, used for SageMaker interactions (default: None). If not
                specified, one is created using the default AWS configuration
                chain.
            serializer (:class:`~sagemaker.serializers.BaseSerializer`): A
                serializer object, used to encode data for an inference endpoint
                (default: :class:`~sagemaker.serializers.IdentitySerializer`).
            deserializer (:class:`~sagemaker.deserializers.BaseDeserializer`): A
                deserializer object, used to decode data from an inference
                endpoint (default: :class:`~sagemaker.deserializers.BytesDeserializer`).
            component_name (str): Name of the Amazon SageMaker inference component
                corresponding the predictor.
        """
        removed_kwargs("content_type", kwargs)
        removed_kwargs("accept", kwargs)
        endpoint_name = renamed_kwargs("endpoint", "endpoint_name", endpoint_name, kwargs)
        self.endpoint_name = endpoint_name
        self.component_name = component_name
        self.sagemaker_session = sagemaker_session or Session()
        self.serializer = serializer
        self.deserializer = deserializer
        self._endpoint_config_name = None
        self._model_names = None
        self._context = None
        self._content_type = None
        self._accept = None

    def predict(
        self,
        data,
        initial_args=None,
        target_model=None,
        target_variant=None,
        inference_id=None,
        custom_attributes=None,
        component_name: Optional[str] = None,
    ):
        """Return the inference from the specified endpoint.

        Args:
            data (object): Input data for which you want the model to provide
                inference. If a serializer was specified when creating the
                Predictor, the result of the serializer is sent as input
                data. Otherwise the data must be sequence of bytes, and the
                predict method then sends the bytes in the request body as is.
            initial_args (dict[str,str]): Optional. Default arguments for boto3
                ``invoke_endpoint`` call. Default is None (no default
                arguments).
            target_model (str): S3 model artifact path to run an inference request on,
                in case of a multi model endpoint. Does not apply to endpoints hosting
                single model (Default: None)
            target_variant (str): The name of the production variant to run an inference
                request on (Default: None). Note that the ProductionVariant identifies the
                model you want to host and the resources you want to deploy for hosting it.
            inference_id (str): If you provide a value, it is added to the captured data
                when you enable data capture on the endpoint (Default: None).
            custom_attributes (str): Provides additional information about a request for an
                inference submitted to a model hosted at an Amazon SageMaker endpoint.
                The information is an opaque value that is forwarded verbatim. You could use this
                value, for example, to provide an ID that you can use to track a request or to
                provide other metadata that a service endpoint was programmed to process. The value
                must consist of no more than 1024 visible US-ASCII characters.

                The code in your model is responsible for setting or updating any custom attributes
                in the response. If your code does not set this value in the response, an empty
                value is returned. For example, if a custom attribute represents the trace ID, your
                model can prepend the custom attribute with Trace ID: in your post-processing
                function (Default: None).
            component_name (str): Optional. Name of the Amazon SageMaker inference component
                corresponding the predictor.

        Returns:
            object: Inference for the given input. If a deserializer was specified when creating
                the Predictor, the result of the deserializer is
                returned. Otherwise the response returns the sequence of bytes
                as is.
        """
        # [TODO]: clean up component_name in _create_request_args
        request_args = self._create_request_args(
            data=data,
            initial_args=initial_args,
            target_model=target_model,
            target_variant=target_variant,
            inference_id=inference_id,
            custom_attributes=custom_attributes,
        )

        inference_component_name = component_name or self._get_component_name()
        if inference_component_name:
            request_args["InferenceComponentName"] = inference_component_name

        response = self.sagemaker_session.sagemaker_runtime_client.invoke_endpoint(**request_args)
        return self._handle_response(response)

    def _handle_response(self, response):
        """Placeholder docstring"""
        response_body = response["Body"]
        content_type = response.get("ContentType", "application/octet-stream")
        return self.deserializer.deserialize(response_body, content_type)

    def _create_request_args(
        self,
        data,
        initial_args=None,
        target_model=None,
        target_variant=None,
        inference_id=None,
        custom_attributes=None,
    ):
        """Placeholder docstring"""

        jumpstart_serialized_data: Optional[Union[str, bytes]] = None
        jumpstart_accept: Optional[str] = None
        jumpstart_content_type: Optional[str] = None

        if isinstance(data, JumpStartSerializablePayload):
            s3_client = self.sagemaker_session.s3_client
            region = self.sagemaker_session._region_name
            bucket = get_jumpstart_content_bucket(region)

            jumpstart_serialized_data = PayloadSerializer(
                bucket=bucket, region=region, s3_client=s3_client
            ).serialize(data)
            jumpstart_content_type = data.content_type
            jumpstart_accept = data.accept

        args = dict(initial_args) if initial_args else {}

        if "EndpointName" not in args:
            args["EndpointName"] = self.endpoint_name

        if "ContentType" not in args:
            if isinstance(data, JumpStartSerializablePayload) and jumpstart_content_type:
                args["ContentType"] = jumpstart_content_type
            else:
                args["ContentType"] = (
                    self.content_type
                    if isinstance(self.content_type, str)
                    else ", ".join(self.content_type)
                )

        if "Accept" not in args:
            if isinstance(data, JumpStartSerializablePayload) and jumpstart_accept:
                args["Accept"] = jumpstart_accept
            else:
                args["Accept"] = (
                    self.accept if isinstance(self.accept, str) else ", ".join(self.accept)
                )

        if target_model:
            args["TargetModel"] = target_model

        if target_variant:
            args["TargetVariant"] = target_variant

        if inference_id:
            args["InferenceId"] = inference_id

        if custom_attributes:
            args["CustomAttributes"] = custom_attributes

        data = (
            jumpstart_serialized_data
            if isinstance(data, JumpStartSerializablePayload) and jumpstart_serialized_data
            else self.serializer.serialize(data)
        )
        if self._get_component_name():
            args["InferenceComponentName"] = self.component_name

        args["Body"] = data
        return args

    def update_endpoint(
        self,
        initial_instance_count=None,
        instance_type=None,
        accelerator_type=None,
        model_name=None,
        tags=None,
        kms_key=None,
        data_capture_config_dict=None,
        max_instance_count=None,
        min_instance_count=None,
        wait=True,
    ):
        """Update the existing endpoint with the provided attributes.

        This creates a new EndpointConfig in the process. If ``initial_instance_count``,
        ``instance_type``, ``accelerator_type``, or ``model_name`` is specified, then a new
        ProductionVariant configuration is created; values from the existing configuration
        are not preserved if any of those parameters are specified.

        Args:
            initial_instance_count (int): The initial number of instances to run in the endpoint.
                This is required if ``instance_type``, ``accelerator_type``, or ``model_name`` is
                specified. Otherwise, the values from the existing endpoint configuration's
                ProductionVariants are used.
            instance_type (str): The EC2 instance type to deploy the endpoint to.
                This is required if ``initial_instance_count`` or ``accelerator_type`` is specified.
                Otherwise, the values from the existing endpoint configuration's
                ``ProductionVariants`` are used.
            accelerator_type (str): The type of Elastic Inference accelerator to attach to
                the endpoint, e.g. "ml.eia1.medium". If not specified, and
                ``initial_instance_count``, ``instance_type``, and ``model_name`` are also ``None``,
                the values from the existing endpoint configuration's ``ProductionVariants`` are
                used. Otherwise, no Elastic Inference accelerator is attached to the endpoint.
            model_name (str): The name of the model to be associated with the endpoint.
                This is required if ``initial_instance_count``, ``instance_type``, or
                ``accelerator_type`` is specified and if there is more than one model associated
                with the endpoint. Otherwise, the existing model for the endpoint is used.
            tags (list[dict[str, str]]): The list of tags to add to the endpoint
                config. If not specified, the tags of the existing endpoint configuration are used.
                If any of the existing tags are reserved AWS ones (i.e. begin with "aws"),
                they are not carried over to the new endpoint configuration.
            kms_key (str): The KMS key that is used to encrypt the data on the storage volume
                attached to the instance hosting the endpoint If not specified,
                the KMS key of the existing endpoint configuration is used.
            data_capture_config_dict (dict): The endpoint data capture configuration
                for use with Amazon SageMaker Model Monitoring. If not specified,
                the data capture configuration of the existing endpoint configuration is used.
            max_instance_count (int): The maximum instance count used for scaling instance.
            min_instance_count (int): The minimum instance count used for scaling instance.

        Raises:
            ValueError: If there is not enough information to create a new ``ProductionVariant``:

                - If ``initial_instance_count``, ``accelerator_type``, or ``model_name`` is
                  specified, but ``instance_type`` is ``None``.
                - If ``initial_instance_count``, ``instance_type``, or ``accelerator_type`` is
                  specified and either ``model_name`` is ``None`` or there are multiple models
                  associated with the endpoint.
        """
        production_variants = None
        current_model_names = self._get_model_names()

        if initial_instance_count or instance_type or accelerator_type or model_name:
            if instance_type is None or initial_instance_count is None:
                raise ValueError(
                    "Missing initial_instance_count and/or instance_type. Provided values: "
                    "initial_instance_count={}, instance_type={}, accelerator_type={}, "
                    "model_name={}.".format(
                        initial_instance_count,
                        instance_type,
                        accelerator_type,
                        model_name,
                    )
                )

            if model_name is None:
                if len(current_model_names) > 1:
                    raise ValueError(
                        "Unable to choose a default model for a new EndpointConfig because "
                        "the endpoint has multiple models: {}".format(
                            ", ".join(current_model_names)
                        )
                    )
                model_name = current_model_names[0]
            else:
                self._model_names = [model_name]

            managed_instance_scaling: Dict[str, int] = {}
            if max_instance_count:
                managed_instance_scaling["MaxInstanceCount"] = max_instance_count
            if min_instance_count:
                managed_instance_scaling["MinInstanceCount"] = min_instance_count

            if managed_instance_scaling and len(managed_instance_scaling) > 0:
                production_variant_config = production_variant(
                    model_name,
                    instance_type,
                    initial_instance_count=initial_instance_count,
                    accelerator_type=accelerator_type,
                    managed_instance_scaling=managed_instance_scaling,
                )
            else:
                production_variant_config = production_variant(
                    model_name,
                    instance_type,
                    initial_instance_count=initial_instance_count,
                    accelerator_type=accelerator_type,
                )
            production_variants = [production_variant_config]

        current_endpoint_config_name = self._get_endpoint_config_name()
        new_endpoint_config_name = name_from_base(current_endpoint_config_name)

        if self._get_component_name():
            endpoint_type = EndpointType.INFERENCE_COMPONENT_BASED
        else:
            endpoint_type = EndpointType.MODEL_BASED

        self.sagemaker_session.create_endpoint_config_from_existing(
            current_endpoint_config_name,
            new_endpoint_config_name,
            new_tags=tags,
            new_kms_key=kms_key,
            new_data_capture_config_dict=data_capture_config_dict,
            new_production_variants=production_variants,
            endpoint_type=endpoint_type,
        )
        self.sagemaker_session.update_endpoint(
            self.endpoint_name, new_endpoint_config_name, wait=wait
        )
        self._endpoint_config_name = new_endpoint_config_name

    def _delete_endpoint_config(self):
        """Delete the Amazon SageMaker endpoint configuration"""
        current_endpoint_config_name = self._get_endpoint_config_name()
        self.sagemaker_session.delete_endpoint_config(current_endpoint_config_name)

    def delete_endpoint(self, delete_endpoint_config=True):
        """Delete the Amazon SageMaker endpoint backing this predictor.

        This also delete the endpoint configuration attached to it if
        delete_endpoint_config is True.

        Args:
            delete_endpoint_config (bool, optional): Flag to indicate whether to
                delete endpoint configuration together with endpoint. Defaults
                to True. If True, both endpoint and endpoint configuration will
                be deleted. If False, only endpoint will be deleted.
        """
        if delete_endpoint_config:
            self._delete_endpoint_config()

        self.sagemaker_session.delete_endpoint(self.endpoint_name)

    def delete_predictor(self, wait: bool = False) -> None:
        """Delete the Amazon SageMaker inference component or endpoint backing this predictor.

        Delete the corresponding inference component if the endpoint is a inference component
        based endpoint.
        Otherwise delete the endpoint where this predictor is hosted.
        """

        if self.component_name:
            self.sagemaker_session.delete_inference_component(self.component_name, wait=wait)
        else:
            self.delete_endpoint()

    def update_predictor(
        self,
        model_name: Optional[str] = None,
        image_uri: Optional[str] = None,
        model_data: Optional[Union[str, dict]] = None,
        env: Optional[Dict[str, str]] = None,
        model_data_download_timeout: Optional[int] = None,
        container_startup_health_check_timeout: Optional[int] = None,
        resources: Optional[ResourceRequirements] = None,
    ):
        """Updates the predictor.

        You can deploy a new Model specification or apply new configurations. The SDK
        applies your updates by updating the inference component that's associated with
        the model.

        Args:
            model_name (Optional[str]): The model name to use to update the
                predictor. (Default: None).
            image_uri (Optional[str]): A Docker image URI. (Default: None).
            model_data (Optional[Union[str, dict]]): Location
                of SageMaker model data. (Default: None).
            env (Optional[dict[str, str]]): Environment variables
                to run with ``image_uri`` when hosted in SageMaker. (Default: None).
            model_data_download_timeout (Optional[int]): The timeout value, in seconds, to download
                and extract model data from Amazon S3 to the individual inference instance
                associated with this production variant. (Default: None).
            container_startup_health_check_timeout (Optional[int]): The timeout value, in seconds,
                for your inference container to pass health check by SageMaker Hosting. For more
                information about health check see:
                https://docs.aws.amazon.com/sagemaker/latest/dg/your-algorithms-inference-code.html#your-algorithms-inference-algo-ping-requests
                (Default: None).
            resources (Optional[ResourceRequirements]): The compute resource requirements
                for a model to be deployed to an endpoint.
                Only EndpointType.INFERENCE_COMPONENT_BASED supports this feature.
                (Default: None).
        """
        if self.component_name is None:
            raise ValueError(
                "No inference component exists for the specified model. "
                "Ensure that you deployed the inference component, and try again."
            )
        # [TODO]: Move to a module
        request = {
            "inference_component_name": self.component_name,
            "specification": {
                "Container": {},
                "StartupParameters": {},
            },
        }

        if resources:
            request["specification"][
                "ComputeResourceRequirements"
            ] = resources.get_compute_resource_requirements()

        # ModelName XOR Container
        if model_name:
            request["specification"]["ModelName"] = model_name
        else:
            if image_uri:
                request["specification"]["Container"]["Image"] = image_uri

            if env:
                request["specification"]["Container"]["Environment"] = env

            if model_data:
                request["specification"]["Container"]["ArtifactUrl"] = model_data

        if resources.copy_count:
            request["runtime_config"] = {"CopyCount": resources.copy_count}

        if model_data_download_timeout:
            request["specification"]["StartupParameters"][
                "ModelDataDownloadTimeoutInSeconds"
            ] = model_data_download_timeout

        if container_startup_health_check_timeout:
            request["specification"]["StartupParameters"][
                "ContainerStartupHealthCheckTimeoutInSeconds"
            ] = container_startup_health_check_timeout

        if "specification" in request:
            empty_keys = []
            for key, value in request["specification"].items():
                if not value:
                    empty_keys.append(key)
            for key in empty_keys:
                del request["specification"][key]

        self.sagemaker_session.update_inference_component(**request)

    def list_related_models(
        self,
        variant_name_equals: Optional[str] = None,
        name_contains: Optional[str] = None,
        creation_time_after: Optional[datetime.datetime] = None,
        creation_time_before: Optional[datetime.datetime] = None,
        last_modified_time_after: Optional[datetime.datetime] = None,
        last_modified_time_before: Optional[datetime.datetime] = None,
        status_equals: Optional[str] = None,
        sort_order: Optional[str] = None,
        sort_by: Optional[str] = None,
        max_results: Optional[int] = None,
        next_token: Optional[str] = None,
    ):
        """List the deployed models co-located with this predictor.

        Calls SageMaker:ListInferenceComponents on the endpoint associated with
            the predictor.

        Args:
            variant_name_equals (str): Optional. A string that matches the name of the
                variant that was assigned to the inference component. (Default: None).
            name_contains (str): Optional. A string that partially matches the names of one or
                more inference components. Filters inference components by name.
                (Default: None).
            creation_time_after (datetime.datetime): Optional. Use this parameter to
                search for inference components created after a specific date and time.
                (Default: None).
            creation_time_before (datetime.datetime): Optional. Use this parameter to
                search for inference components created before a specific date and time.
                (Default: None).
            last_modified_time_after (datetime.datetime): Optional. Use this parameter to
                search for inference components that were last modified after a specific date
                and time. (Default: None).
            last_modified_time_before (datetime.datetime): Optional. Use this parameter to
                search for inference components that were last modified before a specific date
                and time. (Default: None).
            status_equals (str): Optional. The inference component status. Filters
                inference components by status. (Default: None).
            sort_order (str): Optional. The order in which inference components are listed.
                (Default: None).
            sort_order (str): Optional. The order in which inference components are listed in
                the response. (Default: None).
            max_results (int): Optional. The maximum number of results returned by
                list_related_models. (Default: None).
            next_token (str): Optional. A token to resume pagination of list_related_models
                results. (Default: None).

        Returns:
            Tuple[List[Dict[str, Any]], Optional[str]]: A list of Amazon SageMaker inference
                component objects associated with the endpoint. If a next token is returned,
                there are more results available. The value of the next token is a unique
                pagination token.
        """

        response_dict = self.sagemaker_session.list_inference_components(
            endpoint_name_equals=self.endpoint_name,
            variant_name_equals=variant_name_equals,
            name_contains=name_contains,
            creation_time_after=creation_time_after,
            creation_time_before=creation_time_before,
            last_modified_time_after=last_modified_time_after,
            last_modified_time_before=last_modified_time_before,
            status_equals=status_equals,
            sort_order=sort_order,
            sort_by=sort_by,
            max_results=max_results,
            next_token=next_token,
        )

        if len(response_dict) == 0:
            LOGGER.info("No deployed models found for endpoint %s.", self.endpoint_name)
            return {}, None

        # parse list result if returned list inference components are not None
        inference_components = response_dict["InferenceComponents"]
        next_token_from_response = None
        if "NextToken" in response_dict:
            next_token_from_response = response_dict["NextToken"]

        return inference_components, next_token_from_response

    def delete_model(self):
        """Delete the Amazon SageMaker model backing this predictor."""
        request_failed = False
        failed_models = []
        current_model_names = self._get_model_names()
        for model_name in current_model_names:
            try:
                self.sagemaker_session.delete_model(model_name)
            except Exception:  # pylint: disable=broad-except
                request_failed = True
                failed_models.append(model_name)

        if request_failed:
            raise Exception(
                "One or more models cannot be deleted, please retry. \n"
                "Failed models: {}".format(", ".join(failed_models))
            )

    def enable_data_capture(self):
        """Enables data capture by updating DataCaptureConfig.

        This function updates the DataCaptureConfig for the Predictor's associated Amazon SageMaker
        Endpoint to enable data capture. For a more customized experience, refer to
        update_data_capture_config, instead.
        """
        self.update_data_capture_config(
            data_capture_config=DataCaptureConfig(
                enable_capture=True, sagemaker_session=self.sagemaker_session
            )
        )

    def disable_data_capture(self):
        """Disables data capture by updating DataCaptureConfig.

        This function updates the DataCaptureConfig for the Predictor's associated Amazon SageMaker
        Endpoint to disable data capture. For a more customized experience, refer to
        update_data_capture_config, instead.
        """
        self.update_data_capture_config(
            data_capture_config=DataCaptureConfig(
                enable_capture=False, sagemaker_session=self.sagemaker_session
            )
        )

    def update_data_capture_config(self, data_capture_config):
        """Updates the DataCaptureConfig for the Predictor's associated Amazon SageMaker Endpoint.

        Update is done using the provided DataCaptureConfig.

        Args:
            data_capture_config (sagemaker.model_monitor.DataCaptureConfig): The
                DataCaptureConfig to update the predictor's endpoint to use.
        """
        endpoint_desc = self.sagemaker_session.sagemaker_client.describe_endpoint(
            EndpointName=self.endpoint_name
        )

        new_config_name = name_from_base(base=self.endpoint_name)

        data_capture_config_dict = None
        if data_capture_config is not None:
            data_capture_config_dict = data_capture_config._to_request_dict()

        self.sagemaker_session.create_endpoint_config_from_existing(
            existing_config_name=endpoint_desc["EndpointConfigName"],
            new_config_name=new_config_name,
            new_data_capture_config_dict=data_capture_config_dict,
        )

        self.sagemaker_session.update_endpoint(
            endpoint_name=self.endpoint_name, endpoint_config_name=new_config_name
        )

    def list_monitors(self):
        """Generates ModelMonitor objects (or DefaultModelMonitors).

        Objects are generated based on the schedule(s) associated with the endpoint
        that this predictor refers to.

        Returns:
            [sagemaker.model_monitor.model_monitoring.ModelMonitor]: A list of
                ModelMonitor (or DefaultModelMonitor) objects.

        """
        monitoring_schedules_dict = self.sagemaker_session.list_monitoring_schedules(
            endpoint_name=self.endpoint_name
        )
        if len(monitoring_schedules_dict["MonitoringScheduleSummaries"]) == 0:
            print("No monitors found for endpoint. endpoint: {}".format(self.endpoint_name))
            return []

        monitors = []
        for schedule_dict in monitoring_schedules_dict["MonitoringScheduleSummaries"]:
            schedule_name = schedule_dict["MonitoringScheduleName"]
            monitoring_type = schedule_dict.get("MonitoringType")
            clazz = self._get_model_monitor_class(schedule_name, monitoring_type)
            monitors.append(
                clazz.attach(
                    monitor_schedule_name=schedule_name,
                    sagemaker_session=self.sagemaker_session,
                )
            )

        return monitors

    def _get_model_monitor_class(self, schedule_name, monitoring_type):
        """Decide which ModelMonitor class the given schedule should attach to

        Args:
            schedule_name (str): The schedule to be attached.
            monitoring_type (str): The monitoring type of the schedule

        Returns:
            sagemaker.model_monitor.ModelMonitor: ModelMonitor or a subclass of ModelMonitor.

        Raises:
            TypeError: If the class could not be decided (due to unknown monitoring type).
        """
        if monitoring_type == "ModelBias":
            clazz = ModelBiasMonitor
        elif monitoring_type == "ModelExplainability":
            clazz = ModelExplainabilityMonitor
        else:
            schedule = self.sagemaker_session.describe_monitoring_schedule(
                monitoring_schedule_name=schedule_name
            )
            embedded_job_definition = schedule["MonitoringScheduleConfig"].get(
                "MonitoringJobDefinition"
            )
            if embedded_job_definition is not None:  # legacy v1 schedule
                image_uri = embedded_job_definition["MonitoringAppSpecification"]["ImageUri"]
                if image_uri.endswith(DEFAULT_REPOSITORY_NAME):
                    clazz = DefaultModelMonitor
                else:
                    clazz = ModelMonitor
            elif monitoring_type == "DataQuality":
                clazz = DefaultModelMonitor
            elif monitoring_type == "ModelQuality":
                clazz = ModelQualityMonitor
            else:
                raise TypeError("Unknown monitoring type: {}".format(monitoring_type))
        return clazz

    def endpoint_context(self):
        """Retrieves the lineage context object representing the endpoint.

        Examples:
            .. code-block:: python

            predictor = Predictor()
            ...
            context = predictor.endpoint_context()
            models = context.models()

        Returns:
            ContextEndpoint: The context for the endpoint.
        """
        if self._context:
            return self._context

        # retrieve endpoint by name to get arn
        response = self.sagemaker_session.sagemaker_client.describe_endpoint(
            EndpointName=self.endpoint_name
        )
        endpoint_arn = response["EndpointArn"]

        # list context by source uri using arn
        contexts = list(
            EndpointContext.list(sagemaker_session=self.sagemaker_session, source_uri=endpoint_arn)
        )

        if len(contexts) != 0:
            # create endpoint context object
            self._context = EndpointContext.load(
                sagemaker_session=self.sagemaker_session,
                context_name=contexts[0].context_name,
            )

        return self._context

    def _get_endpoint_config_name(self):
        """Placeholder docstring"""
        if self._endpoint_config_name is not None:
            return self._endpoint_config_name
        endpoint_desc = self.sagemaker_session.sagemaker_client.describe_endpoint(
            EndpointName=self.endpoint_name
        )
        self._endpoint_config_name = endpoint_desc["EndpointConfigName"]
        return self._endpoint_config_name

    def _get_model_names(self):
        """Placeholder docstring"""
        if self._model_names is not None:
            return self._model_names
        self._model_names = []

        # If the predictor is for Inference Component, return the model behind the
        # Inference Component. Otherwise, fetch all models behind the Endpoint
        component_name = self._get_component_name()
        if component_name:
            desc = self.sagemaker_session.describe_inference_component(component_name)
            if "Specification" in desc and "ModelName" in desc["Specification"]:
                self._model_names.append(desc["Specification"]["ModelName"])
            return self._model_names

        current_endpoint_config_name = self._get_endpoint_config_name()
        endpoint_config = self.sagemaker_session.sagemaker_client.describe_endpoint_config(
            EndpointConfigName=current_endpoint_config_name
        )
        production_variants = endpoint_config["ProductionVariants"]
        for d in production_variants:
            if "ModelName" in d:
                self._model_names.append(d["ModelName"])
        return self._model_names

    def _get_component_name(self) -> Optional[str]:
        """Get the inference component name field if it exists in the Predictor object."""
        return getattr(self, "component_name", None)

    @property
    def content_type(self):
        """The MIME type of the data sent to the inference endpoint."""
        return self._content_type or self.serializer.CONTENT_TYPE

    @property
    def accept(self):
        """The content type(s) that are expected from the inference endpoint."""
        return self._accept or self.deserializer.ACCEPT

    @content_type.setter
    def content_type(self, val: str):
        """Set the MIME type of the data sent to the inference endpoint."""
        self._content_type = val

    @accept.setter
    def accept(self, val: str):
        """Set the content type(s) that are expected from the inference endpoint."""
        self._accept = val

    @property
    def endpoint(self):
        """Deprecated attribute. Please use endpoint_name."""
        renamed_warning("The endpoint attribute")
        return self.endpoint_name


csv_serializer = deprecated_serialize(CSVSerializer(), "csv_serializer")
json_serializer = deprecated_serialize(JSONSerializer(), "json_serializer")
npy_serializer = deprecated_serialize(NumpySerializer(), "npy_serializer")
csv_deserializer = deprecated_deserialize(CSVDeserializer(), "csv_deserializer")
json_deserializer = deprecated_deserialize(JSONDeserializer(), "json_deserializer")
numpy_deserializer = deprecated_deserialize(NumpyDeserializer(), "numpy_deserializer")
RealTimePredictor = deprecated_class(Predictor, "RealTimePredictor")
