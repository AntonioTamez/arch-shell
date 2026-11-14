"""Template rendering (AD-5): Jinja2 with `StrictUndefined`, output normalized.

Pure: renders template *text* with a context and never touches the filesystem,
clock, randomness, or environment (AD-4). Reading template files is adapter IO
(`archshell.adapters.files.read_text`). There is no loader, so `include`,
`import`, and `extends` are refused.
"""

from __future__ import annotations

from collections.abc import Mapping

import jinja2
import jinja2.meta
from jinja2 import nodes

from archshell.render.normalize import normalize


class RenderError(ValueError):
    """A template that cannot be rendered with the given context.

    `names` lists the undefined variables, sorted, when that is the cause.
    """

    def __init__(self, message: str, names: tuple[str, ...] = ()):
        super().__init__(message)
        self.names = names


_ENV = jinja2.Environment(
    undefined=jinja2.StrictUndefined,
    autoescape=False,
    keep_trailing_newline=True,
)
# AD-4: no randomness. `lipsum` and `random` are Jinja's only nondeterministic
# global and filter; `range`, `dict`, `cycler`, `joiner`, and `namespace` are pure.
del _ENV.globals["lipsum"]
del _ENV.filters["random"]

_LOADER_NODES = (nodes.Include, nodes.Import, nodes.FromImport, nodes.Extends)


def render(template_text: str, context: Mapping[str, object]) -> str:
    """Render `template_text` with `context` and return the normalized text.

    Refuses with `RenderError` when the template uses a name the context does
    not define (checked before rendering, so it holds in branches not taken),
    when a value is undefined at render time (for example a missing attribute),
    when the template is not valid Jinja2 or uses a removed nondeterministic
    global or filter (`lipsum`, `random`), or when any exception is raised
    while rendering.
    """
    try:
        ast = _ENV.parse(template_text)
    except jinja2.TemplateSyntaxError as exc:
        raise RenderError(f"template syntax error at line {exc.lineno}: {exc.message}") from None
    if next(ast.find_all(_LOADER_NODES), None) is not None:
        raise RenderError("include, import, and extends are not supported (no template loader)")
    try:
        undeclared = jinja2.meta.find_undeclared_variables(ast)
    except jinja2.TemplateError as exc:  # e.g. an unknown filter such as `random`
        raise RenderError(f"template error: {exc.message}") from None
    missing = tuple(sorted(undeclared - set(context) - set(_ENV.globals)))
    if missing:
        raise RenderError(f"undefined variable(s): {', '.join(missing)}", missing)
    try:
        output = _ENV.from_string(ast).render(dict(context))
    except jinja2.UndefinedError as exc:
        raise RenderError(f"undefined value: {exc.message}") from None
    except jinja2.TemplateError as exc:
        raise RenderError(f"template error: {exc.message}") from None
    except Exception as exc:  # any failure inside the template is a render error
        raise RenderError(f"error while rendering: {type(exc).__name__}: {exc}") from exc
    return normalize(output)
