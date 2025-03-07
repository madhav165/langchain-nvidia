"""Chat Model Components Derived from ChatModel/NVIDIA"""

from __future__ import annotations

import base64
import enum
import io
import logging
import os
import sys
import urllib.parse
import warnings
from typing import (
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    Literal,
    Optional,
    Sequence,
    Type,
    Union,
)

import requests
from langchain_core.callbacks.manager import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.exceptions import OutputParserException
from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
)
from langchain_core.output_parsers import (
    BaseOutputParser,
    JsonOutputParser,
    PydanticOutputParser,
)
from langchain_core.outputs import (
    ChatGeneration,
    ChatGenerationChunk,
    ChatResult,
    Generation,
)
from langchain_core.pydantic_v1 import BaseModel, Field, PrivateAttr, root_validator
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langchain_core.utils.pydantic import is_basemodel_subclass

from langchain_nvidia_ai_endpoints._common import _NVIDIAClient
from langchain_nvidia_ai_endpoints._statics import Model
from langchain_nvidia_ai_endpoints._utils import convert_message_to_dict

_CallbackManager = Union[AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun]
_DictOrPydanticOrEnumClass = Union[Dict[str, Any], Type[BaseModel], Type[enum.Enum]]
_DictOrPydanticOrEnum = Union[Dict, BaseModel, enum.Enum]

try:
    import PIL.Image

    has_pillow = True
except ImportError:
    has_pillow = False

logger = logging.getLogger(__name__)


def _is_url(s: str) -> bool:
    try:
        result = urllib.parse.urlparse(s)
        return all([result.scheme, result.netloc])
    except Exception as e:
        logger.debug(f"Unable to parse URL: {e}")
        return False


def _resize_image(img_data: bytes, max_dim: int = 1024) -> str:
    if not has_pillow:
        print(  # noqa: T201
            "Pillow is required to resize images down to reasonable scale."
            " Please install it using `pip install pillow`."
            " For now, not resizing; may cause NVIDIA API to fail."
        )
        return base64.b64encode(img_data).decode("utf-8")
    image = PIL.Image.open(io.BytesIO(img_data))
    max_dim_size = max(image.size)
    aspect_ratio = max_dim / max_dim_size
    new_h = int(image.size[1] * aspect_ratio)
    new_w = int(image.size[0] * aspect_ratio)
    resized_image = image.resize((new_w, new_h), PIL.Image.Resampling.LANCZOS)
    output_buffer = io.BytesIO()
    resized_image.save(output_buffer, format="JPEG")
    output_buffer.seek(0)
    resized_b64_string = base64.b64encode(output_buffer.read()).decode("utf-8")
    return resized_b64_string


def _url_to_b64_string(image_source: str) -> str:
    b64_template = "data:image/png;base64,{b64_string}"
    try:
        if _is_url(image_source):
            response = requests.get(
                image_source, headers={"User-Agent": "langchain-nvidia-ai-endpoints"}
            )
            response.raise_for_status()
            encoded = base64.b64encode(response.content).decode("utf-8")
            if sys.getsizeof(encoded) > 200000:
                ## (VK) Temporary fix. NVIDIA API has a limit of 250KB for the input.
                encoded = _resize_image(response.content)
            return b64_template.format(b64_string=encoded)
        elif image_source.startswith("data:image"):
            return image_source
        elif os.path.exists(image_source):
            with open(image_source, "rb") as f:
                encoded = base64.b64encode(f.read()).decode("utf-8")
                return b64_template.format(b64_string=encoded)
        else:
            raise ValueError(
                "The provided string is not a valid URL, base64, or file path."
            )
    except Exception as e:
        raise ValueError(f"Unable to process the provided image source: {e}")


def _nv_vlm_adjust_input(message_dict: Dict[str, Any]) -> Dict[str, Any]:
    """
    The NVIDIA VLM API input message.content:
        {
            "role": "user",
            "content": [
                ...,
                {
                    "type": "image_url",
                    "image_url": "{data}"
                },
                ...
            ]
        }
    where OpenAI VLM API input message.content:
        {
            "role": "user",
            "content": [
                ...,
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "{url | data}"
                    }
                },
                ...
            ]
        }

    This function converts the OpenAI VLM API input message to
    NVIDIA VLM API input message, in place.

    In the process, it accepts a url or file and converts them to
    data urls.
    """
    if content := message_dict.get("content"):
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and "image_url" in part:
                    if (
                        isinstance(part["image_url"], dict)
                        and "url" in part["image_url"]
                    ):
                        part["image_url"] = _url_to_b64_string(part["image_url"]["url"])
    return message_dict


class ChatNVIDIA(BaseChatModel):
    """NVIDIA chat model.

    Example:
        .. code-block:: python

            from langchain_nvidia_ai_endpoints import ChatNVIDIA


            model = ChatNVIDIA(model="meta/llama2-70b")
            response = model.invoke("Hello")
    """

    _client: _NVIDIAClient = PrivateAttr(_NVIDIAClient)
    _default_model_name: str = "meta/llama3-8b-instruct"
    _default_base_url: str = "https://integrate.api.nvidia.com/v1"
    base_url: str = Field(
        description="Base url for model listing an invocation",
    )
    model: Optional[str] = Field(description="Name of the model to invoke")
    temperature: Optional[float] = Field(description="Sampling temperature in [0, 1]")
    max_tokens: Optional[int] = Field(
        1024, description="Maximum # of tokens to generate"
    )
    top_p: Optional[float] = Field(description="Top-p for distribution sampling")
    seed: Optional[int] = Field(description="The seed for deterministic results")
    stop: Optional[Sequence[str]] = Field(description="Stop words (cased)")

    _base_url_var = "NVIDIA_BASE_URL"

    @root_validator(pre=True)
    def _validate_base_url(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        values["base_url"] = (
            values.get(cls._base_url_var.lower())
            or values.get("base_url")
            or os.getenv(cls._base_url_var)
            or cls._default_base_url
        )
        return values

    def __init__(self, **kwargs: Any):
        """
        Create a new NVIDIAChat chat model.

        This class provides access to a NVIDIA NIM for chat. By default, it
        connects to a hosted NIM, but can be configured to connect to a local NIM
        using the `base_url` parameter. An API key is required to connect to the
        hosted NIM.

        Args:
            model (str): The model to use for chat.
            nvidia_api_key (str): The API key to use for connecting to the hosted NIM.
            api_key (str): Alternative to nvidia_api_key.
            base_url (str): The base URL of the NIM to connect to.
                            Format for base URL is http://host:port
            temperature (float): Sampling temperature in [0, 1].
            max_tokens (int): Maximum number of tokens to generate.
            top_p (float): Top-p for distribution sampling.
            seed (int): A seed for deterministic results.
            stop (list[str]): A list of cased stop words.

        API Key:
        - The recommended way to provide the API key is through the `NVIDIA_API_KEY`
            environment variable.
        """
        super().__init__(**kwargs)
        self._client = _NVIDIAClient(
            base_url=self.base_url,
            model_name=self.model,
            default_hosted_model_name=self._default_model_name,
            api_key=kwargs.get("nvidia_api_key", kwargs.get("api_key", None)),
            infer_path="{base_url}/chat/completions",
            cls=self.__class__.__name__,
        )
        # todo: only store the model in one place
        # the model may be updated to a newer name during initialization
        self.model = self._client.model_name

    @property
    def available_models(self) -> List[Model]:
        """
        Get a list of available models that work with ChatNVIDIA.
        """
        return self._client.get_available_models(self.__class__.__name__)

    @classmethod
    def get_available_models(
        cls,
        **kwargs: Any,
    ) -> List[Model]:
        """
        Get a list of available models that work with ChatNVIDIA.
        """
        return cls(**kwargs).available_models

    @property
    def _llm_type(self) -> str:
        """Return type of NVIDIA AI Foundation Model Interface."""
        return "chat-nvidia-ai-playground"

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        inputs = [
            _nv_vlm_adjust_input(message)
            for message in [convert_message_to_dict(message) for message in messages]
        ]
        payload = self._get_payload(inputs=inputs, stop=stop, stream=False, **kwargs)
        response = self._client.get_req(payload=payload)
        responses, _ = self._client.postprocess(response)
        self._set_callback_out(responses, run_manager)
        parsed_response = self._custom_postprocess(responses, streaming=False)
        # for pre 0.2 compatibility w/ ChatMessage
        # ChatMessage had a role property that was not present in AIMessage
        parsed_response.update({"role": "assistant"})
        generation = ChatGeneration(message=AIMessage(**parsed_response))
        return ChatResult(generations=[generation], llm_output=responses)

    def _stream(
        self,
        messages: List[BaseMessage],
        stop: Optional[Sequence[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        """Allows streaming to model!"""
        inputs = [
            _nv_vlm_adjust_input(message)
            for message in [convert_message_to_dict(message) for message in messages]
        ]
        payload = self._get_payload(inputs=inputs, stop=stop, stream=True, **kwargs)
        for response in self._client.get_req_stream(payload=payload):
            self._set_callback_out(response, run_manager)
            parsed_response = self._custom_postprocess(response, streaming=True)
            # for pre 0.2 compatibility w/ ChatMessageChunk
            # ChatMessageChunk had a role property that was not
            # present in AIMessageChunk
            # unfortunately, AIMessageChunk does not have extensible propery
            # parsed_response.update({"role": "assistant"})
            message = AIMessageChunk(**parsed_response)
            chunk = ChatGenerationChunk(message=message)
            if run_manager:
                run_manager.on_llm_new_token(chunk.text, chunk=chunk)
            yield chunk

    def _set_callback_out(
        self,
        result: dict,
        run_manager: Optional[_CallbackManager],
    ) -> None:
        result.update({"model_name": self.model})
        if run_manager:
            for cb in run_manager.handlers:
                if hasattr(cb, "llm_output"):
                    cb.llm_output = result

    def _custom_postprocess(
        self, msg: dict, streaming: bool = False
    ) -> dict:  # todo: remove
        kw_left = msg.copy()
        out_dict = {
            "role": kw_left.pop("role", "assistant") or "assistant",
            "name": kw_left.pop("name", None),
            "id": kw_left.pop("id", None),
            "content": kw_left.pop("content", "") or "",
            "additional_kwargs": {},
            "response_metadata": {},
        }
        # "tool_calls" is set for invoke and stream responses
        if tool_calls := kw_left.pop("tool_calls", None):
            assert isinstance(
                tool_calls, list
            ), "invalid response from server: tool_calls must be a list"
            # todo: break this into post-processing for invoke and stream
            if not streaming:
                out_dict["additional_kwargs"]["tool_calls"] = tool_calls
            elif streaming:
                out_dict["tool_call_chunks"] = []
                for tool_call in tool_calls:
                    # todo: the nim api does not return the function index
                    #       for tool calls in stream responses. this is
                    #       an issue that needs to be resolved server-side.
                    #       the only reason we can skip this for now
                    #       is because the nim endpoint returns only full
                    #       tool calls, no deltas.
                    # assert "index" in tool_call, (
                    #     "invalid response from server: "
                    #     "tool_call must have an 'index' key"
                    # )
                    assert "function" in tool_call, (
                        "invalid response from server: "
                        "tool_call must have a 'function' key"
                    )
                    out_dict["tool_call_chunks"].append(
                        {
                            "index": tool_call.get("index", None),
                            "id": tool_call.get("id", None),
                            "name": tool_call["function"].get("name", None),
                            "args": tool_call["function"].get("arguments", None),
                        }
                    )
        # we only create the response_metadata from the last message in a stream.
        # if we do it for all messages, we'll end up with things like
        # "model_name" = "mode-xyz" * # messages.
        if "finish_reason" in kw_left:
            out_dict["response_metadata"] = kw_left
        return out_dict

    ######################################################################################
    ## Core client-side interfaces

    def _get_payload(
        self, inputs: Sequence[Dict], **kwargs: Any
    ) -> dict:  # todo: remove
        """Generates payload for the _NVIDIAClient API to send to service."""
        messages: List[Dict[str, Any]] = []
        for msg in inputs:
            if isinstance(msg, str):
                # (WFH) this shouldn't ever be reached but leaving this here bcs
                # it's a Chesterton's fence I'm unwilling to touch
                messages.append(dict(role="user", content=msg))
            elif isinstance(msg, dict):
                if not msg.get("role") == "assistant":
                    if msg.get("content", None) is None:
                        # content=None is valid for assistant messages (tool calling)
                        raise ValueError(f"Message {msg} has no content.")
                messages.append(msg)
            else:
                raise ValueError(f"Unknown message received: {msg} of type {type(msg)}")

        # special handling for "stop" because it always comes in kwargs.
        # if user provided "stop" to invoke/stream, it will be non-None
        # in kwargs.
        # note: we cannot tell if the user specified stop=None to invoke/stream because
        #       the default value of stop is None.
        # todo: remove self.stop
        assert "stop" in kwargs, '"stop" param is expected in kwargs'
        if kwargs["stop"] is None:
            kwargs.pop("stop")

        # setup default payload values
        payload: Dict[str, Any] = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "top_p": self.top_p,
            "seed": self.seed,
            "stop": self.stop,
        }

        # merge incoming kwargs with attr_kwargs giving preference to
        # the incoming kwargs
        payload.update(kwargs)

        # remove keys with None values from payload
        payload = {k: v for k, v in payload.items() if v is not None}

        return {"messages": messages, **payload}

    def bind_tools(
        self,
        tools: Sequence[Union[Dict[str, Any], Type[BaseModel], Callable, BaseTool]],
        *,
        tool_choice: Optional[
            Union[dict, str, Literal["auto", "none", "any", "required"], bool]
        ] = None,
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, BaseMessage]:
        """
        Bind tools to the model.

        Args:
            tools (list): A list of tools to bind to the model.
            tool_choice (Optional[Union[dict,
                                        str,
                                        Literal["auto", "none", "any", "required"],
                                        bool]]):
               Control tool choice.
                 "any" and "required" - force a tool call.
                 "auto" - let the model decide.
                 "none" - force no tool call.
                 string or dict - force a specific tool call.
                 bool - if True, force a tool call; if False, force no tool call.
               Defaults to passing no value.
            **kwargs: Additional keyword arguments.

        see https://python.langchain.com/v0.1/docs/modules/model_io/chat/function_calling/#request-forcing-a-tool-call
        """
        # check if the model supports tools, warn if it does not
        if self._client.model and not self._client.model.supports_tools:
            warnings.warn(
                f"Model '{self.model}' is not known to support tools. "
                "Your tool binding may fail at inference time."
            )

        tool_name = None
        if isinstance(tool_choice, bool):
            tool_choice = "required" if tool_choice else "none"
        elif isinstance(tool_choice, str):
            # LangChain documents "any" as an option, server API uses "required"
            if tool_choice == "any":
                tool_choice = "required"
            # if a string that's not "auto", "none", or "required"
            # then it must be a tool name
            if tool_choice not in ["auto", "none", "required"]:
                tool_name = tool_choice
                tool_choice = {
                    "type": "function",
                    "function": {"name": tool_choice},
                }
        elif isinstance(tool_choice, dict):
            # if a dict, it must be a tool choice dict, e.g.
            #  {"type": "function", "function": {"name": "my_tool"}}
            if "type" not in tool_choice:
                tool_choice["type"] = "function"
            if "function" not in tool_choice:
                raise ValueError("Tool choice dict must have a 'function' key")
            if "name" not in tool_choice["function"]:
                raise ValueError("Tool choice function dict must have a 'name' key")
            tool_name = tool_choice["function"]["name"]

        # check that the specified tool is in the tools list
        tool_dicts = [convert_to_openai_tool(tool) for tool in tools]
        if tool_name:
            if not any(tool["function"]["name"] == tool_name for tool in tool_dicts):
                raise ValueError(
                    f"Tool choice '{tool_name}' not found in the tools list"
                )

        return super().bind(
            tools=tool_dicts,
            tool_choice=tool_choice,
            **kwargs,
        )

    def bind_functions(
        self,
        functions: Sequence[Union[Dict[str, Any], Type[BaseModel], Callable]],
        function_call: Optional[str] = None,
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, BaseMessage]:
        raise NotImplementedError("Not implemented, use `bind_tools` instead.")

    # we have an Enum extension to BaseChatModel.with_structured_output and
    # as a result need to type ignore for the schema parameter and return type.
    def with_structured_output(  # type: ignore
        self,
        schema: _DictOrPydanticOrEnumClass,
        *,
        include_raw: bool = False,
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, _DictOrPydanticOrEnum]:
        """
        Bind a structured output schema to the model.

        The schema can be -
         0. a dictionary representing a JSON schema
         1. a Pydantic object
         2. an Enum

        0. If a dictionary is provided, the model will return a dictionary. Example:
        ```
        json_schema = {
            "title": "joke",
            "description": "Joke to tell user.",
            "type": "object",
            "properties": {
                "setup": {
                    "type": "string",
                    "description": "The setup of the joke",
                },
                "punchline": {
                    "type": "string",
                    "description": "The punchline to the joke",
                },
            },
            "required": ["setup", "punchline"],
        }

        structured_llm = llm.with_structured_output(json_schema)
        structured_llm.invoke("Tell me a joke about NVIDIA")
        # Output: {'setup': 'Why did NVIDIA go broke? The hardware ate all the software.',
        #          'punchline': 'It took a big bite out of their main board.'}
        ```

        1. If a Pydantic schema is provided, the model will return a Pydantic object.
           Example:
        ```
        from langchain_core.pydantic_v1 import BaseModel, Field
        class Joke(BaseModel):
            setup: str = Field(description="The setup of the joke")
            punchline: str = Field(description="The punchline to the joke")

        structured_llm = llm.with_structured_output(Joke)
        structured_llm.invoke("Tell me a joke about NVIDIA")
        # Output: Joke(setup='Why did NVIDIA go broke? The hardware ate all the software.',
        #              punchline='It took a big bite out of their main board.')
        ```

        2. If an Enum is provided, all values must be strings, and the model will return
           an Enum object. Example:
        ```
        import enum
        class Choices(enum.Enum):
            A = "A"
            B = "B"
            C = "C"

        structured_llm = llm.with_structured_output(Choices)
        structured_llm.invoke("What is the first letter in this list? [X, Y, Z, C]")
        # Output: <Choices.C: 'C'>
        ```

        Note about streaming: Unlike other streaming responses, the streamed chunks
        will be increasingly complete. They will not be deltas. The last chunk will
        contain the complete response.

        For instance with a dictionary schema, the chunks will be:
        ```
        structured_llm = llm.with_structured_output(json_schema)
        for chunk in structured_llm.stream("Tell me a joke about NVIDIA"):
            print(chunk)

        # Output:
        # {}
        # {'setup': ''}
        # {'setup': 'Why'}
        # {'setup': 'Why did'}
        # {'setup': 'Why did N'}
        # {'setup': 'Why did NVID'}
        # ...
        # {'setup': 'Why did NVIDIA go broke? The hardware ate all the software.', 'punchline': 'It took a big bite out of their main board'}
        # {'setup': 'Why did NVIDIA go broke? The hardware ate all the software.', 'punchline': 'It took a big bite out of their main board.'}
        ```

        For instnace with a Pydantic schema, the chunks will be:
        ```
        structured_llm = llm.with_structured_output(Joke)
        for chunk in structured_llm.stream("Tell me a joke about NVIDIA"):
            print(chunk)

        # Output:
        # setup='Why did NVIDIA go broke? The hardware ate all the software.' punchline=''
        # setup='Why did NVIDIA go broke? The hardware ate all the software.' punchline='It'
        # setup='Why did NVIDIA go broke? The hardware ate all the software.' punchline='It took'
        # ...
        # setup='Why did NVIDIA go broke? The hardware ate all the software.' punchline='It took a big bite out of their main board'
        # setup='Why did NVIDIA go broke? The hardware ate all the software.' punchline='It took a big bite out of their main board.'
        ```

        For Pydantic schema and Enum, the output will be None if the response is
        insufficient to construct the object or otherwise invalid. For instance,
        ```
        llm = ChatNVIDIA(max_tokens=1)
        structured_llm = llm.with_structured_output(Joke)
        print(structured_llm.invoke("Tell me a joke about NVIDIA"))

        # Output: None
        ```

        For more, see https://python.langchain.com/v0.2/docs/how_to/structured_output/
        """  # noqa: E501

        if "method" in kwargs:
            warnings.warn(
                "The 'method' parameter is unnecessary and is ignored. "
                "The appropriate method will be chosen automatically depending "
                "on the type of schema provided."
            )

        if include_raw:
            raise NotImplementedError(
                "include_raw=True is not implemented, consider "
                "https://python.langchain.com/v0.2/docs/how_to/"
                "structured_output/#prompting-and-parsing-model"
                "-outputs-directly or rely on the structured response "
                "being None when the LLM produces an incomplete response."
            )

        # check if the model supports structured output, warn if it does not
        known_good = False
        # todo: we need to store model: Model in this class
        #       instead of model: str (= Model.id)
        #  this should be: if not self.model.supports_tools: warnings.warn...
        candidates = [
            model for model in self.available_models if model.id == self.model
        ]
        if not candidates:  # user must have specified the model themselves
            known_good = False
        else:
            assert len(candidates) == 1, "Multiple models with the same id"
            known_good = candidates[0].supports_structured_output is True
        if not known_good:
            warnings.warn(
                f"Model '{self.model}' is not known to support structured output. "
                "Your output may fail at inference time."
            )

        if isinstance(schema, dict):
            output_parser: BaseOutputParser = JsonOutputParser()
            nvext_param: Dict[str, Any] = {"guided_json": schema}

        elif issubclass(schema, enum.Enum):
            # langchain's EnumOutputParser is not in langchain_core
            # and doesn't support streaming. this is a simple implementation
            # that supports streaming with our semantics of returning None
            # if no complete object can be constructed.
            class EnumOutputParser(BaseOutputParser):
                enum: Type[enum.Enum]

                def parse(self, response: str) -> Any:
                    try:
                        return self.enum(response.strip())
                    except ValueError:
                        pass
                    return None

            # guided_choice only supports string choices
            choices = [choice.value for choice in schema]
            if not all(isinstance(choice, str) for choice in choices):
                # instead of erroring out we could coerce the enum values to
                # strings, but would then need to coerce them back to their
                # original type for Enum construction.
                raise ValueError(
                    "Enum schema must only contain string choices. "
                    "Use StrEnum or ensure all member values are strings."
                )
            output_parser = EnumOutputParser(enum=schema)
            nvext_param = {"guided_choice": choices}

        elif is_basemodel_subclass(schema):
            # PydanticOutputParser does not support streaming. what we do
            # instead is ignore all inputs that are incomplete wrt the
            # underlying Pydantic schema. if the entire input is invalid,
            # we return None.
            class ForgivingPydanticOutputParser(PydanticOutputParser):
                def parse_result(
                    self, result: List[Generation], *, partial: bool = False
                ) -> Any:
                    try:
                        return super().parse_result(result, partial=partial)
                    except OutputParserException:
                        pass
                    return None

            output_parser = ForgivingPydanticOutputParser(pydantic_object=schema)
            nvext_param = {"guided_json": schema.schema()}

        else:
            raise ValueError(
                "Schema must be a Pydantic object, a dictionary "
                "representing a JSON schema, or an Enum."
            )

        return super().bind(nvext=nvext_param) | output_parser
