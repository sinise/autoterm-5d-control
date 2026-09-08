# Autoterm Heater

Controls an Autoterm-family diesel heater (and its comfort panel) directly
from Home Assistant over MQTT, using a reverse-engineered UART protocol.
Confirmed against a real Autoterm 5D / Flow 5 (BINAR-5S) unit. See the
Documentation tab for wiring requirements and safety notes before installing.

This add-on **owns both serial ports directly** -- it replaces the
standalone `autoterm_web.py` script from the
[main repo](https://github.com/sinise/autoterm-heater-control), it does not
run alongside it. The physical comfort panel keeps working normally the
whole time as a manual fallback.
