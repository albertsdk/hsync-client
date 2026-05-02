# Log Labels

`hsync-client` tags every log line with a bracketed action label (e.g.
`[DOWNLOAD]`, `[NO MASTER]`) to show what kind of operation it is.

The **canonical catalog** — every label, its meaning, its color category, and
which project emits it — is the single source in
[`hsync-common/docs/log-labels.md`](https://github.com/albertosdk/hsync-common/blob/main/docs/log-labels.md):
the client emits the labels marked with `✓` in the **C** column.

- Run `hsync --help-symbols` to print the legend directly in the terminal.
- Log levels (ERROR, WARNING, INFO, DEBUG) are shown by the logging system and
  are independent of the action labels.
