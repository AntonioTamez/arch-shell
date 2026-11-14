---
description: Ask archshell-engine for the next action of the built-in probe flow and show the reply.
allowed-tools: Bash(${ARCHSHELL_ENGINE:-archshell-engine} next:*), Bash(echo:*)
---

Engine reply (the engine command is `$ARCHSHELL_ENGINE` when set, else `archshell-engine`):

!`echo '{}' | ${ARCHSHELL_ENGINE:-archshell-engine} next --feature probe 2>>"${TMPDIR:-/tmp}/archshell-engine.log"`

Show the engine reply above to the user verbatim, in a JSON code block. Do not interpret, change, or act on it.
