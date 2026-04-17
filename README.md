# Kidney Perfusion Control System v3.6.5

A high-resilience clinical console for ex-vivo kidney perfusion, running on Raspberry Pi 5.
Includes PID controllers for perfusate temperature and arterial pressure with user selectable
targets.  Safety features include watchdogs for all major peripherals and stall detection / recovery 
for the blood pump.  The thermal cut-out of the motor is also monitored for safety.

"This software is designed with a 'Fail-Safe' priority. Communication loss or hardware stalls do not result in 'frozen' states; instead, the system actively de-energizes actuators (heaters/pumps) to prevent injury or equipment damage."

## Hardware Architecture
- **Host:** Raspberry Pi 5 (2GB)
- **Comm Interface:** ES-279 (Ethernet-to-Serial)
- **Blood Pump:** TMCM 163 Port 9001 (Binary TCP/IP)
- **Terumo BGA:** CDI 500 Port 9002 (Tab-delimited Serial)
- **Syringe Drivers:** New ERA Ports 9003 & 9004 (ASCII Serial)
- **Board 2 (Gas):** Port 9005 (PWM Duty Control)
- **Board 1 (Thermal):** Port 9008 (Bitmask GPIO Control)

## Key Features
-Key Features

Clinical Safety and Interlock System:
Continuous monitoring of all peripherals including Blood Pump, Terumo, and Boards 1 and 2. The system automatically disables the heater and moves to a safe state if data packets are lost for more than 5 seconds. PID logic includes strict saturation limits to prevent thermal runaway. Real-time UI LEDs provide visual confirmation of system health and connectivity.

Intelligent Stall Recovery:
If a blood pump stall is detected, the software executes a reverse-commutation pulse to break mechanical stiction or Hall-effect sensor dead zones before resuming the setpoint. If recovery fails after 10 seconds of persistent stall, the system issues a global Emergency Stop to protect motor windings and vessel integrity.

Precision Perfusion Control:
Features dual-mode PID control. Pressure Mode provides automated RPM adjustment to maintain target mmHg, while Temperature Mode provides high-resolution PWM control for blood warming. Setpoints are synchronized across all control loops at 1Hz to ensure zero-lag response.

Data Integrity and Logging:
Sub-second parsing of pH, pCO2, pO2, pressure, and flow rates. Optimized for clinical environments by allowing users to log CSV data directly to external USB drives. Generates 5-minute diagnostic heartbeat summaries to the terminal for long-term reliability tracking.

Edge Deployment on Raspberry Pi 5:
Application runs within virtual environment at startup. 
OS hardening is tailored for appliance mode with disabled power-saving and screen blanking. Full support for battery-backed Real-Time Clocks ensures accurate log timestamps in air-gapped networks.

## Installation
1. Ensure `python3-tk` and `python3-matplotlib` are installed.
2. Configure ES-279 to the static IP `192.168.127.254`.
3. To run in virtual environment on Pi5 rename to main.py and move to the kidney_app folder.  launch.sh in the autorun folder will start the application automatically and recovery in the event of crash.
4. Run `main.py`.  # should happen automatically
