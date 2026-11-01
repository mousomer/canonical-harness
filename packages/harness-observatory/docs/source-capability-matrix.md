# Observed source capability matrix

Qualitative findings from a bounded convenience sample of local session formats. These findings describe capabilities, not historical usage or prevalence. No transcripts, source paths, corpus counts or observation date ranges are retained.

| Information | Codex | Claude | Reliability / notes |
|---|---|---|---|
| session ID | direct: session_meta payload id/session_id | direct: sessionId | Per-file identity; branch/sidechain semantics need care |
| timestamps | direct: outer timestamp | direct: timestamp | Some administrative rows have none |
| model | direct: turn_context.model | direct: message.model | Can change within a session; first observed model is not whole-run identity |
| prompts | direct: response_item message input_text | direct: user message content | Mirrors/attachments excluded; text omitted at safe boundary |
| assistant messages | direct: response_item output_text | direct: assistant text blocks | Reasoning/encrypted content excluded |
| tool calls | direct: function_call/custom_tool_call | direct: tool_use | Correlation via call_id/tool_use_id |
| shell commands | direct: exec_command arguments.cmd | direct: Bash input.command | Nested orchestrator code is opaque |
| command results | direct: function_call_output | direct: tool_result | Output may be a yielded process, not final exit |
| token counts | direct but variable: token_count/info and token_usage_record | direct: message.usage | Aggregation unreliable without dedup/cumulative semantics; v0.1 leaves totals unknown |
| cwd/repository | cwd direct; git.repository_url sometimes direct | cwd direct | cwd does not prove repository identity |
| branch | direct when session_meta.git.branch present | direct: gitBranch | A recorded label does not prove historical branch tip |
| Git SHA | direct when git.commit_hash present | unavailable as dedicated field in sample | Command text is not verified state; no timestamp guessing |
| duration | derivable observed timestamp span | derivable observed timestamp span | Not active compute duration or total run duration |

Codex observed record families also include compacted, world_state, inter_agent_communication_metadata and token_usage_record. event_msg mirrors messages and contains per-turn task_started/task_complete; these are not session completion. Claude includes queue-operation, attachment, system, title, mode and bridge metadata. Unsupported records produce structured warnings rather than disappearing silently. Claude user rows containing tool_result are not human turns. No cost, reliable session finish, final SHA, uncommitted changes or author-attributed commit set is assumed.
