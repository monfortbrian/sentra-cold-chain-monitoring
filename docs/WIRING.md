# Hardware Wiring Guide

## Important: Order of Operations

1. Finish all software setup first
2. Power off the Pi (`sudo shutdown -h now`, wait for green LED to stop, unplug)
3. Stack PiJuice HAT onto the Pi
4. Connect DS18B20 sensor
5. Power on and verify

---

## PiJuice HAT Installation

The PiJuice stacks directly on top of the Raspberry Pi GPIO header. It passes through all 40 GPIO pins, meaning your temperature sensor still has access to the pins it needs.

1. Align the PiJuice header with all 40 GPIO pins on the Pi
2. Press down firmly and evenly until fully seated
3. Connect the Li-ion battery cable to the PiJuice battery connector
4. The PiJuice LED will indicate: green = charged, blue = charging, red = low

---

## DS18B20 Temperature Sensor Wiring

Your sensor has 3 wires inside the black cable:

| Wire Color | Function     | Connect To    |
| ---------- | ------------ | ------------- |
| RED        | Power (VCC)  | Pin 1 (3.3V)  |
| BLACK      | Ground (GND) | Pin 6 (GND)   |
| YELLOW     | Data         | Pin 7 (GPIO4) |

### With PiJuice Installed

The PiJuice passes through all GPIO pins. The pins you need (1, 6, 7) are accessible on top of the PiJuice header. Connect the sensor wires to the PiJuice pass-through header, not directly to the Pi.

### Pin Layout (top view, USB ports facing down)

```
           3.3V [1]  [2] 5V        ← RED wire goes to pin 1
          GPIO2 [3]  [4] 5V
          GPIO3 [5]  [6] GND       ← BLACK wire goes to pin 6
    DATA→ GPIO4 [7]  [8] GPIO14    ← YELLOW wire goes to pin 7
            GND [9] [10] GPIO15
```

### How to Connect Without a Breadboard

Since you have bare wire ends and no breadboard, you have these options:

**Option A: Female-to-female jumper wires (recommended)**
Buy a pack of female-to-female dupont jumper wires. Strip your sensor wires slightly, twist each one to a jumper wire, wrap with electrical tape. Plug the female end onto the GPIO pin.

**Option B: Direct pin insertion**
If your sensor wires are thin and solid core, you can carefully push them directly onto the GPIO header pins. This is fragile and not recommended for permanent deployment, but works for testing.

**Option C: Screw terminal breakout board**
A GPIO screw terminal adapter board (~$5) lets you screw bare wires into labeled terminals. Best for permanent deployment.

### Pull-up Resistor

The DS18B20 requires a 4.7k ohm resistor between the RED (3.3V) and YELLOW (Data) wires.

- If your sensor came on a small PCB module with 3 pins/terminals: resistor is built in, skip this
- If your sensor is a bare metal probe with 3 loose wires: you need to add the resistor

---

## Waveshare USB to RS485 Adapter

The RS485 adapter is NOT needed for the DS18B20 temperature sensor. They use different protocols:

- DS18B20 uses 1-Wire protocol via GPIO pins
- RS485 is for industrial equipment (energy meters, solar inverters, PLCs)

The Waveshare adapter has 3 terminals: GND, A+, B-. These connect to RS485-compatible industrial devices. You will use this in a future phase for:

- Modbus energy meters
- Solar charge controllers
- Industrial temperature controllers
- HVAC systems

For now, set it aside. It is not part of the Sentra v1 deployment.

---

## After Wiring: Verification

### Power on the Pi and SSH back in

```bash
ssh admin@<Pi-IP>
```

### Check temperature sensor

```bash
ls /sys/bus/w1/devices/
```

Expected output: `28-xxxxxxxxxxxx  w1_bus_master1`

If you see the `28-` folder, the sensor is detected. Read it:

```bash
cat /sys/bus/w1/devices/28-*/w1_slave
```

Expected output includes `t=XXXXX` where XXXXX is temperature in millidegrees (e.g., `t=4700` means 4.7 degrees C).

### Check PiJuice

```bash
i2cdetect -y 1
```

You should see address `14` in the grid. This confirms PiJuice is communicating.

### Restart the monitor to pick up the sensor

```bash
sudo systemctl restart sentra-monitor
sudo systemctl restart sentra-api
```

The dashboard will now show real temperature data instead of demo data.

---

## When to Reboot the Raspberry Pi

Reboot is required after:

- Enabling 1-Wire or I2C interfaces in raspi-config
- Installing PiJuice HAT (new I2C device)
- Kernel updates via apt upgrade
- Any changes to /boot/firmware/config.txt

Reboot is NOT required after:

- Editing Python scripts (just restart the systemd service)
- Changing the dashboard HTML (just refresh browser)
- Starting or stopping Docker containers
- Changing n8n workflows

To reboot:

```bash
sudo reboot
```

Wait 2 minutes, then SSH back in.
