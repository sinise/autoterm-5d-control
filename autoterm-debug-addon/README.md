# Autoterm Heater Debug

Everything the [Autoterm Heater](../autoterm-addon/README.md) add-on does,
plus optional extended-telemetry probing (across 19 vendor heater
profiles) and a downloadable raw traffic capture log, for continuing the
UART protocol reverse-engineering. See the Documentation tab before
enabling debug mode -- it involves sending an experimental handshake frame
toward the heater on the live bus (the known issue where the heater's
reply confused the physical panel is fixed as of 1.1.0, but treat it as
still experimental beyond that).

This add-on **owns both serial ports directly**, same as the regular one --
install **either** the regular Autoterm Heater add-on **or** this one,
never both at the same time.
