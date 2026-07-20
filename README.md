# Logitech G923 (PlayStation) RPM LED Fix

This repository contains a fixed version of **forza-wheel-leds** for the Logitech G923 PlayStation/PC edition.

## What's fixed?

The original project opened the wrong HID interface for the G923 PlayStation model.

This version opens the correct interface:

- HID Interface: MI_00
- Usage Page: 0x01
- Usage: 0x04

As a result, the RPM LEDs now work correctly in Forza Horizon 5.

## Tested with

- Logitech G923 PlayStation/PC
- Windows 11
- Python 3.12
- Forza Horizon 5

## Credits

Original project:
- forza-wheel-leds contributors (by 23sigma)

G923 PlayStation HID interface fix:
- FGX228/FGM228

Debugging assistance:
- ChatGPT (OpenAI)

## License

This project remains licensed under the MIT License.




Special thanks to the original project authors for creating the foundation that made this fix possible.