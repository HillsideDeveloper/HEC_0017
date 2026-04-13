# Kidney Perfusion Control System v3.3.0

A high-resilience clinical console for ex-vivo kidney perfusion, running on Raspberry Pi 5.
Includes PID controllers for perfusate temperature and arterial pressure with user selectable
targets.

## Hardware Architecture
- **Host:** Raspberry Pi 5 (4GB)
- **Comm Interface:** ES-279 (Ethernet-to-Serial)
- **Blood Pump:** TMCM 163 Port 9001 (Binary TCP/IP)
- **Terumo BGA:** CDI 500 Port 9002 (Tab-delimited Serial)
- **Syringe Drivers:** New ERA Ports 9003 & 9004 (ASCII Serial)
- **Board 2 (Gas):** Port 9005 (PWM Duty Control)
- **Board 1 (Thermal):** Port 9008 (Bitmask GPIO Control)

## Key Features
- **Self-Healing safe_comm:** Prevents UI deadlocks using 2.0s lock timeouts.
- **24-Hour Trend:** Real-time Matplotlib visualization of Flow variance.
- **1Hz Logging:** Synchronized disk writing for precise clinical audit.
- **Watchdog:** Active 60s monitoring and recovery of infusion pumps.
- **PID controllers:** PID controllers for pressure and temperature

## Installation
1. Ensure `python3-tk` and `python3-matplotlib` are installed.
2. Configure ES-279 to the static IP `192.168.127.254`.
3. Run `python3 Version3_1_0.py`.
