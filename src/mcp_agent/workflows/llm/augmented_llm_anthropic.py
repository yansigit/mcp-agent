from typing import Iterable, List, Type, Union

from pydantic import BaseModel

from anthropic import Anthropic
from anthropic.types import (
    ContentBlock,
    DocumentBlockParam,
    Message,
    MessageParam,
    ImageBlockParam,
    TextBlock,
    TextBlockParam,
    ToolParam,
    ToolResultBlockParam,
    ToolUseBlockParam,
    Base64ImageSourceParam,
    PlainTextSourceParam,
    Base64PDFSourceParam,
    ThinkingBlockParam,
    RedactedThinkingBlockParam,
)
from mcp.types import (
    CallToolRequestParams,
    CallToolRequest,
    EmbeddedResource,
    ImageContent,
    ModelPreferences,
    StopReason,
    TextContent,
    TextResourceContents,
)

# from mcp_agent import console
# from mcp_agent.agents.agent import HUMAN_INPUT_TOOL_NAME
from mcp_agent.config import AnthropicSettings
from mcp_agent.executor.workflow_task import workflow_task
from mcp_agent.utils.common import ensure_serializable, typed_dict_extras, to_string
from mcp_agent.utils.pydantic_type_serializer import serialize_model, deserialize_model
from mcp_agent.workflows.llm.augmented_llm import (
    AugmentedLLM,
    ModelT,
    MCPMessageParam,
    MCPMessageResult,
    ProviderToMCPConverter,
    RequestParams,
    CallToolResult,
)
from mcp_agent.logging.logger import get_logger

MessageParamContent = Union[
    str,
    Iterable[
        Union[
            TextBlockParam,
            ImageBlockParam,
            ToolUseBlockParam,
            ToolResultBlockParam,
            DocumentBlockParam,
            ThinkingBlockParam,
            RedactedThinkingBlockParam,
            ContentBlock,
        ]
    ],
]


class AnthropicAugmentedLLM(AugmentedLLM[MessageParam, Message]):
    """
    The basic building block of agentic systems is an LLM enhanced with augmentations
    such as retrieval, tools, and memory provided from a collection of MCP servers.
    Our current models can actively use these capabilities—generating their own search queries,
    selecting appropriate tools, and determining what information to retain.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(
            *args,
            type_converter=AnthropicMCPTypeConverter,
            **kwargs,
        )

        self.provider = "Anthropic"
        # Initialize logger with name if available
        self.logger = get_logger(f"{__name__}.{self.name}" if self.name else __name__)

        self.model_preferences = self.model_preferences or ModelPreferences(
            costPriority=0.3,
            speedPriority=0.4,
            intelligencePriority=0.3,
        )

        default_model = "claude-3-7-sonnet-latest"  # Fallback default

        if self.context.config.anthropic:
            if hasattr(self.context.config.anthropic, "default_model"):
                default_model = self.context.config.anthropic.default_model
        self.default_request_params = self.default_request_params or RequestParams(
            model=default_model,
            modelPreferences=self.model_preferences,
            maxTokens=2048,
            systemPrompt=self.instruction,
            parallel_tool_calls=False,
            max_iterations=10,
            use_history=True,
        )

    async def generate(
        self,
        message,
        request_params: RequestParams | None = None,
    ):
        """
        Process a query using an LLM and available tools.
        The default implementation uses Claude as the LLM.
        Override this method to use a different LLM.
        """
        config = self.context.config
        messages: List[MessageParam] = []
        params = self.get_request_params(request_params)

        if params.use_history:
            messages.extend(self.history.get())

        if isinstance(message, str):
            messages.append({"role": "user", "content": message})
        elif isinstance(message, list):
            messages.extend(message)
        else:
            messages.append(message)

        response = await self.agent.list_tools()
        available_tools: List[ToolParam] = [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.inputSchema,
            }
            for tool in response.tools
        ]

        responses: List[Message] = []
        model = await self.select_model(params)

        for i in range(params.max_iterations):
            if i == params.max_iterations - 1 and response.stop_reason == "tool_use":
                final_prompt_message = MessageParam(
                    role="user",
                    content="""We've reached the maximum number of iterations. 
                    Please stop using tools now and provide your final comprehensive answer based on all tool results so far. 
                    At the beginning of your response, clearly indicate that your answer may be incomplete due to reaching the maximum number of tool usage iterations, 
                    and explain what additional information you would have needed to provide a more complete answer.""",
                )
                messages.append(final_prompt_message)

            arguments = {
                "model": model,
                "max_tokens": params.maxTokens,
                "messages": messages,
                "system": self.instruction or params.systemPrompt,
                "stop_sequences": params.stopSequences,
                "tools": available_tools,
            }

            if params.metadata:
                arguments = {**arguments, **params.metadata}

            self.logger.debug(f"{arguments}")
            self._log_chat_progress(chat_turn=(len(messages) + 1) // 2, model=model)

            request = ensure_serializable(
                RequestCompletionRequest(
                    config=config.anthropic,
                    payload=arguments,
                )
            )

            response: Message = await self.executor.execute(
                AnthropicCompletionTasks.request_completion_task, request
            )

            if isinstance(response, BaseException):
                self.logger.error(f"Error: {response}")
                break

            self.logger.debug(
                f"{model} response:",
                data=response,
            )

            response_as_message = self.convert_message_to_message_param(response)
            messages.append(response_as_message)
            responses.append(response)

            if response.stop_reason == "end_turn":
                self.logger.debug(
                    f"Iteration {i}: Stopping because finish_reason is 'end_turn'"
                )
                break
            elif response.stop_reason == "stop_sequence":
                # We have reached a stop sequence
                self.logger.debug(
                    f"Iteration {i}: Stopping because finish_reason is 'stop_sequence'"
                )
                break
            elif response.stop_reason == "max_tokens":
                # We have reached the max tokens limit
                self.logger.debug(
                    f"Iteration {i}: Stopping because finish_reason is 'max_tokens'"
                )
                # TODO: saqadri - would be useful to return the reason for stopping to the caller
                break
            else:  # response.stop_reason == "tool_use":
                for content in response.content:
                    if content.type == "tool_use":
                        tool_name = content.name
                        tool_args = content.input
                        tool_use_id = content.id

                        # TODO -- productionize this
                        # if tool_name == HUMAN_INPUT_TOOL_NAME:
                        #     # Get the message from the content list
                        #     message_text = ""
                        #     for block in response_as_message["content"]:
                        #         if (
                        #             isinstance(block, dict)
                        #             and block.get("type") == "text"
                        #         ):
                        #             message_text += block.get("text", "")
                        #         elif hasattr(block, "type") and block.type == "text":
                        #             message_text += block.text

                        # panel = Panel(
                        #     message_text,
                        #     title="MESSAGE",
                        #     style="green",
                        #     border_style="bold white",
                        #     padding=(1, 2),
                        # )
                        # console.console.print(panel)

                        tool_call_request = CallToolRequest(
                            method="tools/call",
                            params=CallToolRequestParams(
                                name=tool_name, arguments=tool_args
                            ),
                        )

                        result = await self.call_tool(
                            request=tool_call_request, tool_call_id=tool_use_id
                        )

                        message = self.from_mcp_tool_result(result, tool_use_id)

                        messages.append(message)

        if params.use_history:
            self.history.set(messages)

        self._log_chat_finished(model=model)

        return responses

    async def generate_str(
        self,
        message,
        request_params: RequestParams | None = None,
    ) -> str:
        """
        Process a query using an LLM and available tools.
        The default implementation uses Claude as the LLM.
        Override this method to use a different LLM.
        """
        responses: List[Message] = await self.generate(
            message=message,
            request_params=request_params,
        )

        final_text: List[str] = []

        for response in responses:
            for content in response.content:
                if content.type == "text":
                    final_text.append(content.text)
                elif content.type == "tool_use":
                    final_text.append(
                        f"[Calling tool {content.name} with args {content.input}]"
                    )

        return "\n".join(final_text)

    async def generate_structured(
        self,
        message,
        response_model: Type[ModelT],
        request_params: RequestParams | None = None,
    ) -> ModelT:
        # First we invoke the LLM to generate a string response
        # We need to do this in a two-step process because Instructor doesn't
        # know how to invoke MCP tools via call_tool, so we'll handle all the
        # processing first and then pass the final response through Instructor
        response = await self.generate_str(
            message=message,
            request_params=request_params,
        )

        params = self.get_request_params(request_params)
        model = await self.select_model(params)

        serialized_response_model: str | None = None

        if self.executor and self.executor.execution_engine == "temporal":
            # Serialize the response model to a string
            serialized_response_model = serialize_model(response_model)

        structured_response = await self.executor.execute(
            AnthropicCompletionTasks.request_structured_completion_task,
            RequestStructuredCompletionRequest(
                config=self.context.config.anthropic,
                params=params,
                response_model=response_model
                if not serialized_response_model
                else None,
                serialized_response_model=serialized_response_model,
                response_str=response,
                model=model,
            ),
        )

        # TODO: saqadri (MAC) - fix request_structured_completion_task to return ensure_serializable
        # Convert dict back to the proper model instance if needed
        if isinstance(structured_response, dict):
            structured_response = response_model.model_validate(structured_response)

        return structured_response

    @classmethod
    def convert_message_to_message_param(
        cls, message: Message, **kwargs
    ) -> MessageParam:
        """Convert a response object to an input parameter object to allow LLM calls to be chained."""
        content = []

        for content_block in message.content:
            if content_block.type == "text":
                content.append(TextBlockParam(type="text", text=content_block.text))
            elif content_block.type == "tool_use":
                content.append(
                    ToolUseBlockParam(
                        type="tool_use",
                        name=content_block.name,
                        input=content_block.input,
                        id=content_block.id,
                    )
                )

        return MessageParam(role="assistant", content=content, **kwargs)

    def message_param_str(self, message: MessageParam) -> str:
        """Convert an input message to a string representation."""

        if message.get("content"):
            content = message["content"]
            if isinstance(content, str):
                return content
            else:
                final_text: List[str] = []
                for block in content:
                    if block.text:
                        final_text.append(str(block.text))
                    else:
                        final_text.append(str(block))

                return "\n".join(final_text)

        return str(message)

    def message_str(self, message: Message) -> str:
        """Convert an output message to a string representation."""
        content = message.content

        if content:
            if isinstance(content, list):
                final_text: List[str] = []
                for block in content:
                    if block.text:
                        final_text.append(str(block.text))
                    else:
                        final_text.append(str(block))

                return "\n".join(final_text)
            else:
                return str(content)

        return str(message)


class RequestCompletionRequest(BaseModel):
    config: AnthropicSettings
    payload: dict


class RequestStructuredCompletionRequest(BaseModel):
    config: AnthropicSettings
    params: RequestParams
    response_model: Type[ModelT] | None = None
    serialized_response_model: str | None = None
    response_str: str
    model: str


class AnthropicCompletionTasks:
    @staticmethod
    @workflow_task
    async def request_completion_task(
        request: RequestCompletionRequest,
    ) -> Message:
        """
        Request a completion from Anthropic's API.
        """

        anthropic = Anthropic(api_key=request.config.api_key)

        payload = request.payload
        response = anthropic.messages.create(**payload)
        response = ensure_serializable(response)
        return response

    @staticmethod
    @workflow_task
    async def request_structured_completion_task(
        request: RequestStructuredCompletionRequest,
    ):
        """
        Request a structured completion using Instructor's Anthropic API.
        """
        import instructor

        if request.response_model:
            response_model = request.response_model
        elif request.serialized_response_model:
            response_model = deserialize_model(request.serialized_response_model)
        else:
            raise ValueError(
                "Either response_model or serialized_response_model must be provided for structured completion."
            )

        # We pass the text through instructor to extract structured data
        client = instructor.from_anthropic(
            Anthropic(api_key=request.config.api_key),
        )

        # Extract structured data from natural language
        structured_response = client.chat.completions.create(
            model=request.model,
            response_model=response_model,
            messages=[{"role": "user", "content": request.response_str}],
            max_tokens=request.params.maxTokens,
        )

        return structured_response


class AnthropicMCPTypeConverter(ProviderToMCPConverter[MessageParam, Message]):
    """
    Convert between Anthropic and MCP types.
    """

    @classmethod
    def from_mcp_message_result(cls, result: MCPMessageResult) -> Message:
        # MCPMessageResult -> Message
        if result.role != "assistant":
            raise ValueError(
                f"Expected role to be 'assistant' but got '{result.role}' instead."
            )

        return Message(
            role="assistant",
            type="message",
            content=[mcp_content_to_anthropic_content(result.content)],
            model=result.model,
            stop_reason=mcp_stop_reason_to_anthropic_stop_reason(result.stopReason),
            id=result.id or None,
            usage=result.usage or None,
            # TODO: should we push extras?
        )

    @classmethod
    def to_mcp_message_result(cls, result: Message) -> MCPMessageResult:
        # Message -> MCPMessageResult

        contents = anthropic_content_to_mcp_content(result.content)
        if len(contents) > 1:
            raise NotImplementedError(
                "Multiple content elements in a single message are not supported in MCP yet"
            )
        mcp_content = contents[0]

        return MCPMessageResult(
            role=result.role,
            content=mcp_content,
            model=result.model,
            stopReason=anthropic_stop_reason_to_mcp_stop_reason(result.stop_reason),
            # extras for Message fields
            **result.model_dump(exclude={"role", "content", "model", "stop_reason"}),
        )

    @classmethod
    def from_mcp_message_param(cls, param: MCPMessageParam) -> MessageParam:
        # MCPMessageParam -> MessageParam
        extras = param.model_dump(exclude={"role", "content"})
        return MessageParam(
            role=param.role,
            content=[
                mcp_content_to_anthropic_content(param.content, for_message_param=True)
            ],
            **extras,
        )

    @classmethod
    def to_mcp_message_param(cls, param: MessageParam) -> MCPMessageParam:
        # Implement the conversion from ChatCompletionMessage to MCP message param

        contents = anthropic_content_to_mcp_content(param.content)

        # TODO: saqadri - the mcp_content can have multiple elements
        # while sampling message content has a single content element
        # Right now we error out if there are > 1 elements in mcp_content
        # We need to handle this case properly going forward
        if len(contents) > 1:
            raise NotImplementedError(
                "Multiple content elements in a single message are not supported"
            )
        mcp_content = contents[0]

        return MCPMessageParam(
            role=param.role,
            content=mcp_content,
            **typed_dict_extras(param, ["role", "content"]),
        )

    @classmethod
    def from_mcp_tool_result(
        cls, result: CallToolResult, tool_use_id: str
    ) -> MessageParam:
        """Convert mcp tool result to user MessageParam"""
        tool_result_block_content: list[TextBlockParam | ImageBlockParam] = []

        for content in result.content:
            converted_content = mcp_content_to_anthropic_content(
                content, for_message_param=True
            )
            if converted_content["type"] in ["text", "image"]:
                tool_result_block_content.append(converted_content)

        if not tool_result_block_content:
            # If no valid content, return as error
            tool_result_block_content = [
                TextBlockParam(type="text", text="No result returned")
            ]
            result.isError = True

        return MessageParam(
            role="user",
            content=[
                ToolResultBlockParam(
                    type="tool_result",
                    tool_use_id=tool_use_id,
                    content=tool_result_block_content,
                    is_error=result.isError,
                )
            ],
        )


def mcp_content_to_anthropic_content(
    content: TextContent | ImageContent | EmbeddedResource,
    for_message_param: bool = False,
) -> ContentBlock | MessageParamContent:
    """
    Converts MCP content types into Anthropic-compatible content blocks.

    Args:
        content (TextContent | ImageContent | EmbeddedResource): The MCP content to convert.
        for_message_param (bool, optional): If True, returns Anthropic message param content types.
                                    If False, returns Anthropic response message content types.
                                    Defaults to False.

    Returns:
        ContentBlock: The converted content block in Anthropic format.
    """
    if for_message_param:
        if isinstance(content, TextContent):
            return TextBlockParam(type="text", text=content.text)
        elif isinstance(content, ImageContent):
            return ImageBlockParam(
                type="image",
                source=Base64ImageSourceParam(
                    type="base64",
                    data=content.data,
                    media_type=content.mimeType,
                ),
            )
        elif isinstance(content, EmbeddedResource):
            if isinstance(content.resource, TextResourceContents):
                return TextBlockParam(type="text", text=content.resource.text)
            else:
                if content.resource.mimeType == "text/plain":
                    source = PlainTextSourceParam(
                        type="text",
                        data=content.resource.blob,
                        mimeType=content.resource.mimeType,
                    )
                elif content.resource.mimeType == "application/pdf":
                    source = Base64PDFSourceParam(
                        type="base64",
                        data=content.resource.blob,
                        mimeType=content.resource.mimeType,
                    )
                else:
                    # Best effort to convert
                    return TextBlockParam(
                        type="text",
                        text=f"{content.resource.mimeType}:{content.resource.blob}",
                    )
                return DocumentBlockParam(
                    type="document",
                    source=source,
                )
    else:
        if isinstance(content, TextContent):
            return TextBlock(type=content.type, text=content.text)
        elif isinstance(content, ImageContent):
            # Best effort to convert an image to text (since there's no ImageBlock)
            return TextBlock(type="text", text=f"{content.mimeType}:{content.data}")
        elif isinstance(content, EmbeddedResource):
            if isinstance(content.resource, TextResourceContents):
                return TextBlock(type="text", text=content.resource.text)
            else:  # BlobResourceContents
                return TextBlock(
                    type="text",
                    text=f"{content.resource.mimeType}:{content.resource.blob}",
                )
        else:
            # Last effort to convert the content to a string
            return TextBlock(type="text", text=str(content))


def anthropic_content_to_mcp_content(
    content: str
    | Iterable[
        TextBlockParam
        | ImageBlockParam
        | ToolUseBlockParam
        | ToolResultBlockParam
        | DocumentBlockParam
        | ContentBlock
    ],
) -> List[TextContent | ImageContent | EmbeddedResource]:
    mcp_content = []

    if isinstance(content, str):
        mcp_content.append(TextContent(type="text", text=content))
    else:
        for block in content:
            if block.type == "text":
                mcp_content.append(TextContent(type="text", text=block.text))
            elif block.type == "image":
                raise NotImplementedError("Image content conversion not implemented")
            elif block.type == "tool_use":
                # Best effort to convert a tool use to text (since there's no ToolUseContent)
                mcp_content.append(
                    TextContent(
                        type="text",
                        text=to_string(block),
                    )
                )
            elif block.type == "tool_result":
                # Best effort to convert a tool result to text (since there's no ToolResultContent)
                mcp_content.append(
                    TextContent(
                        type="text",
                        text=to_string(block),
                    )
                )
            elif block.type == "document":
                raise NotImplementedError("Document content conversion not implemented")
            else:
                # Last effort to convert the content to a string
                mcp_content.append(TextContent(type="text", text=str(block)))

    return mcp_content


def mcp_stop_reason_to_anthropic_stop_reason(stop_reason: StopReason):
    if not stop_reason:
        return None
    elif stop_reason == "endTurn":
        return "end_turn"
    elif stop_reason == "maxTokens":
        return "max_tokens"
    elif stop_reason == "stopSequence":
        return "stop_sequence"
    elif stop_reason == "toolUse":
        return "tool_use"
    else:
        return stop_reason


def anthropic_stop_reason_to_mcp_stop_reason(stop_reason: str) -> StopReason:
    if not stop_reason:
        return None
    elif stop_reason == "end_turn":
        return "endTurn"
    elif stop_reason == "max_tokens":
        return "maxTokens"
    elif stop_reason == "stop_sequence":
        return "stopSequence"
    elif stop_reason == "tool_use":
        return "toolUse"
    else:
        return stop_reason
