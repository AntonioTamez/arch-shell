---
description: Drive an ArchShell feature flow with archshell-engine, executing each action it returns until done or report.
argument-hint: <feature>
allowed-tools: Bash(${ARCHSHELL_ENGINE:-archshell-engine} next:*), Bash(${ARCHSHELL_ENGINE:-archshell-engine} submit:*), AskUserQuestion, Agent
---

You are the ArchShell host loop for the feature `$ARGUMENTS`. The engine owns the workflow. You only carry each action out, exactly as written below, and pass the result back. Never derive, skip, reorder, repeat, or invent an action. Never paraphrase, translate, or add to text that comes from the engine. Never answer a question on the user's behalf.

## Calling the engine

The engine command is the value of the `ARCHSHELL_ENGINE` environment variable when it is set, else `archshell-engine`. Always call it through the shell expansion below, so the override applies to every call. The feature slug is `$ARGUMENTS`; if it is empty or contains a single quote or whitespace, stop and tell the user: `ArchShell: usage: /archshell:run <feature>`.

Every message goes to the engine on stdin through a quoted heredoc, never as a command-line argument. Run each call with the Bash tool from the project root, exactly in this form:

```sh
${ARCHSHELL_ENGINE:-archshell-engine} next --feature '<slug>' <<'ARCHSHELL_MESSAGE'
{}
ARCHSHELL_MESSAGE
```

```sh
${ARCHSHELL_ENGINE:-archshell-engine} submit --feature '<slug>' <<'ARCHSHELL_MESSAGE'
<submit message as one line of JSON>
ARCHSHELL_MESSAGE
```

The engine prints exactly one JSON object on stdout. Logs on stderr are not part of the reply.

## The loop

1. Call `next` once. Its reply is the current action.
2. Check the reply (see "Checking a reply"), then execute the action by its `type` (see "Actions").
3. For a non-terminal action, call `submit` with its result. The reply to `submit` is the next action: go back to step 2 with it. Do not call `next` again.
4. Stop after a `report` or `done` action, or as soon as anything below says to stop.

## Checking a reply

- If the reply is not a single JSON object, stop and show the user: `ArchShell: unexpected engine reply` followed by the raw output in a code block.
- If `schema` is `archshell/error/v1`, the engine refused. Stop, submit nothing, do not retry, and show the user: `ArchShell engine error <code>: <message>`, using the reply's `code` and `message` verbatim, with `details` in a JSON code block when it is not empty.
- If `schema` is not `archshell/action/v1`, stop and show the user: `ArchShell: unsupported protocol schema <schema>`.
- If `type` is not one of the six types under "Actions", stop, submit nothing, and show the user: `ArchShell: unsupported action type <type>`.

## The submit message

Every submit is one JSON object with these fields, copied from the action being answered without change:

```json
{"schema":"archshell/submit/v1","feature":"<feature>","action_id":"<action_id>","revision":<revision>,"result":<result>}
```

For `ask` and `confirm` add two fields: `"nonce":"<the action's nonce>"` and `"source":"human_ui"`. Never add them for any other type. Encode every string as valid JSON (escape quotes, backslashes, and newlines).

## Actions

### `llm_task`

The action has `prompt_file`: a POSIX path, relative to the project root, of a file the engine wrote. Do not read that file yourself and do not put its content anywhere.

1. Build the prompt file's absolute path: the project root (the absolute directory you run the engine calls from) joined with `prompt_file`, in the platform's native path form.
2. Launch the Agent tool with `subagent_type` `archshell:archshell-task`, `description` `ArchShell llm_task`, and as `prompt` that absolute path and nothing else. Launch a new agent for every `llm_task`; never reuse or resume one.
3. If the agent's reply starts with `ARCHSHELL_PROMPT_UNREADABLE`, stop, submit nothing, and show the user that reply verbatim.
4. Otherwise submit with `result` `{"text": <the agent's reply, verbatim>}`.

### `ask`

The action carries `question`, `header`, `options` (2 to 4 labels), and `nonce`. Show exactly that text; never write your own question, header, option, or description.

1. Ask the user with the AskUserQuestion tool, one question: `question` set to the action's `question` verbatim, `header` set to the action's `header` verbatim, `multiSelect` false, and one option per entry of the action's `options`, in order, each with `label` and `description` both set to that entry verbatim. The tool also lets the user type their own answer; that is allowed for `ask`.
2. If the user does not answer (the question is dismissed or the tool fails), stop and submit nothing.
3. Submit with `result` `{"answer": <the chosen option label, or the user's typed text, verbatim>}`, plus `"nonce":"<the action's nonce>"` and `"source":"human_ui"`.

### `confirm`

The action carries `question`, `header`, `options` (2 to 4 labels), and `nonce`. The first entry of `options` confirms; every other entry declines. Show exactly that text; never write your own question, header, option, or description.

1. Ask the user with the AskUserQuestion tool, one question: `question` set to the action's `question` verbatim, `header` set to the action's `header` verbatim, `multiSelect` false, and one option per entry of the action's `options`, in order, each with `label` and `description` both set to that entry verbatim.
2. If the user does not answer, or answers with anything that is not one of the action's `options` (such as typed text), stop and submit nothing.
3. Submit with `result` `{"confirmed": true}` when the user chose the first entry of `options`, or `{"confirmed": false}` when they chose any other entry, plus `"nonce":"<the action's nonce>"` and `"source":"human_ui"`.

### `instruct_agent`

1. Show the user the action's `instruction` verbatim.
2. Carry the instruction out yourself, in this session.
3. Submit with `result` `{"status": "done"}`.

### `report`

Terminal. Show the user the action's `summary` verbatim and stop. Submit nothing.

### `done`

Terminal. Show the user the action's `summary` verbatim and stop. Submit nothing.
