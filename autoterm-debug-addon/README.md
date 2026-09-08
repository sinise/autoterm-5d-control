# Autoterm 5D Debug

Everything the [Autoterm 5D](../autoterm-addon/README.md) add-on does, plus
optional extended-telemetry probing and a downloadable raw traffic capture
log, for continuing the UART protocol reverse-engineering. See the
Documentation tab before enabling debug mode -- it involves sending an
unconfirmed-safe handshake frame on the live bus.

This add-on **owns both serial ports directly**, same as the regular one --
install **either** the regular Autoterm 5D add-on **or** this one, never
both at the same time.
