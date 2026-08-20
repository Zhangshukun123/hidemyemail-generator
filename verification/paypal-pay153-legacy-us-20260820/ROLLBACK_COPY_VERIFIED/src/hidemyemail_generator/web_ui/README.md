# Web UI architecture

The account workspace is a self-contained frontend assembled by
`PageBuilder`. Templates, shared design tokens, page styles, and controllers
are separate package resources so the backend only serves completed HTML.

## Patterns

- **Builder** — `PageBuilder` composes templates, CSS, and JavaScript.
- **API Gateway** — `ApiGateway` owns authentication headers, JSON handling,
  and login-expiry behavior.
- **Observer** — `ObservableStore` provides immutable state snapshots and
  notifies renderers after state changes.
- **Router** — `HashRouter` maps a URL hash to one registered view strategy.
- **Command** — `CommandBus` maps `data-action` values to asynchronous
  commands and centralizes errors and busy states.
- **Presentation layer** — `WorkspaceRenderer` only turns state into UI;
  `WorkspaceController` coordinates loading, polling, and user commands.

## Adding a view

1. Add the view section and navigation route to `templates/app.html`.
2. Register its title and route callback in `static/app.js`.
3. Add page-specific layout rules to `static/app.css`; reuse tokens and base
   components from `static/base.css`.
4. Keep network calls in `ApiGateway` commands and rendering in
   `WorkspaceRenderer`.
5. Extend `tests/test_web_ui.py` and verify the page in a local browser.
