#!/bin/bash
# Quick baud-rate discovery for an unknown UART device.
#
# Sweeps common baud rates on each port and dumps a few lines of raw hex so
# you can eyeball which rate produces clean, framed-looking bytes (a 0xAA
# start byte recurring at sensible intervals) versus garbage. This is how
# 2400 8N1 was originally identified for the Autoterm 5D bus -- useful again
# if you're bringing this up on different hardware or a different heater
# model.
#
# Edit the port list below to match your wiring.

PORTS="/dev/ttyUSB1 /dev/ttyUSB3"
BAUDS="300 600 1200 2400 4800 9600 19200 38400"

for port in $PORTS; do
  echo "########## $port ##########"
  for baud in $BAUDS; do
    echo "=== $port @ $baud ==="
    stty -F "$port" "$baud" raw
    timeout 3 cat "$port" | xxd | head -10
  done
done
