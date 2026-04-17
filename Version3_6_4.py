# --- VERSION 3.6.4 ---
# 1. FIXED: Added setpoint synchronization for Temperature PID in the master loop.
# 2. FIXED: Swapped Air Pump and Gas Valve variable mapping to match hardware wiring. DV Changed 14/04/26
# 3. SAFETY: Heater interlock forces 0 PWM if Pump fails or RPM < 150.
# 4. TUNING: Thermal Anti-windup limit maintained at 1500.
# 5. UI: Manual heater overrides removed; PWM scale is now a read-only indicator.
# 6. Now checking for motor stall or Drive Over Temperature Flag and adding safety features.
# 7. Added some logic to supress pump errors when idle not stalled.
# 8. Now includes stall recovery

import tkinter as tk
from tkinter import scrolledtext, filedialog, messagebox
import socket, threading, re, struct, csv, os
from datetime import datetime
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

# Network Config
ES_IP = "192.168.127.254"
PORT_BLOOD_PUMP = 9001
PORT_TERUMO = 9002
PORT_UPPER_SYRINGE = 9003
PORT_LOWER_SYRINGE = 9004
PORT_BOARD_2 = 9005 
PORT_BOARD_1 = 9008 
SYRINGE_DIA = "21.69"  #Internal diameter of a bd plastic 30ml syringe 

class PID:
    def __init__(self, kp, ki, kd, setpoint, output_limits=(None, None), windup_limit=500):
        self.kp, self.ki, self.kd = kp, ki, kd
        self.setpoint = setpoint
        self.integral = 0
        self.last_error = 0
        self.min_out, self.max_out = output_limits
        self.windup_limit = windup_limit

    def update(self, measurement, dt=1.0):
        error = self.setpoint - measurement
        self.integral = max(min(self.integral + (error * dt), self.windup_limit), -self.windup_limit)
        derivative = (error - self.last_error) / dt
        output = (self.kp * error) + (self.ki * self.integral) + (self.kd * derivative)
        self.last_error = error
        if self.min_out is not None: output = max(self.min_out, output)
        if self.max_out is not None: output = min(self.max_out, output)
        return output

class ClinicalConsole:
    def __init__(self, root):
        self.root = root
        self.root.title("Kidney Device Console v3.6.4")
        self.root.geometry("1450x980")
        
        # --- UI Data State ---
        self.ph_val = "--.--"; self.po2_val = "--.--"; self.pco2_val = "--.--"
        self.temp_val = "0.00"; self.press_val = "0.00"; self.flow_val = "0.00"
        self.actual_rpm = 0
        
        # Health Tracking Flags
        self.port_status = {"Pump": True, "Terumo": True, "Board1": True}
        self.health_counts = {"Terumo": 0, "Board1": 0, "BloodPump": 0}
        self.motor_stalled = False
        self.motor_overheat = False
        self.pump_active = False # New flag to prevent idle stall warnings
        self.last_b1_receive_time = datetime.now() # track actual data arrival
        self.recovery_in_progress = False  # NEW: Priority lock for safety commands
        
        
        # PID Controllers
        self.auto_mode = tk.BooleanVar(value=False)
        self.press_setpoint = tk.DoubleVar(value=60.0)
        self.press_pid = PID(kp=1.5, ki=0.05, kd=0.2, setpoint=60.0, windup_limit=500)
        
        self.temp_auto_mode = tk.BooleanVar(value=False)
        self.temp_setpoint = tk.DoubleVar(value=37.0)
        # Thermal anti-windup limit maintained at 1500
        self.temp_pid = PID(kp=15.0, ki=0.2, kd=4.0, setpoint=37.0, output_limits=(0, 240), windup_limit=1500)
        
        self.last_b1_send_time = datetime.now()
        self.last_terumo_packet_time = datetime.now()
        
        # Hardware/Graphing State
        self.flow_history = []; self.time_history = []
        self.max_graph_points = 288 
        self.air_pump_pct = tk.IntVar(value=0); self.gas_valve_pct = tk.IntVar(value=0)
        self.heater_pwm = tk.IntVar(value=0) 

        self.terumo_active = False; self.is_logging = False; self.log_counter = 0 
        self.syringe_states = {PORT_UPPER_SYRINGE: "STOP", PORT_LOWER_SYRINGE: "STOP"}
        self.syringe_rates = {PORT_UPPER_SYRINGE: "5.0", PORT_LOWER_SYRINGE: "5.0"}
        
        self.cmd_lock = threading.Lock()
        self.create_layout()
        self.refresh_ui_labels()
        
        # Launch Threads
        threading.Thread(target=self.terumo_listener, daemon=True).start()
        threading.Thread(target=self.board_one_listener, daemon=True).start()
        threading.Thread(target=self.blood_pump_loop, daemon=True).start()
        threading.Thread(target=self.master_control_loop, daemon=True).start()
        threading.Thread(target=self.start_health_monitor_thread, daemon=True).start()
        threading.Thread(target=self.start_syringe_watchdog_thread, daemon=True).start()
        self.check_heartbeat_status()

    # --- MASTER CONTROL LOOP (1Hz) ---
    def master_control_loop(self):
        while True:
            # Sync Setpoints from UI to PID Instance
            try:
                self.press_pid.setpoint = float(self.press_setpoint.get())
                self.temp_pid.setpoint = float(self.temp_setpoint.get())
            except ValueError: pass

            # 1. Pressure PID
            b1_age = (datetime.now() - self.last_b1_send_time).total_seconds()
            if self.auto_mode.get() and b1_age < 5.0:
                try:
                    p_adj = self.press_pid.update(float(self.press_val))
                    new_rpm = max(min(self.actual_rpm + int(p_adj), 3500), 0)
                    if abs(p_adj) > 2:
                        self.send_pump_cmd(new_rpm)
                except: pass

            # 2. Temperature PID (Interlock: Pump must be OK and moving > 150 RPM)
            if self.temp_auto_mode.get() and self.port_status["Pump"] and self.actual_rpm > 150:
                try:
                    t_out = self.temp_pid.update(float(self.temp_val))
                    self.heater_pwm.set(int(t_out))
                except: pass
            else:
                self.heater_pwm.set(0)

            # 3. Watchdog Pulse
            if (datetime.now() - self.last_b1_send_time).total_seconds() > 2.0:
                self.send_b1_cmd()
            
            threading.Event().wait(1.0)

    # --- COMMUNICATIONS ---
     def safe_comm(self, port, payload, rx_len=0):
        with self.cmd_lock:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    # Proposed Hardening
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
                
                    s.settimeout(1.2) # Consistent with 3.6.3 timeout
                    s.connect((ES_IP, port))
                    s.sendall(payload)

                    # Set success flags only if connect/send succeeded
                    if port == PORT_BLOOD_PUMP: self.port_status["Pump"] = True
                    if port == PORT_BOARD_1: self.port_status["Board1"] = True

                    if rx_len > 0: 
                        return s.recv(rx_len)
                    else: 
                        return s.recv(1024)
            except:
                # Explicitly set failure flags for immediate interlock response
                if port == PORT_BLOOD_PUMP: self.port_status["Pump"] = False
                if port == PORT_BOARD_1: self.port_status["Board1"] = False
                return None

    def send_b1_cmd(self):
        self.last_b1_send_time = datetime.now()
        g = 0x08 # Fixed Heat Direction
        pay = f"{str(g).zfill(3)}{str(self.heater_pwm.get()).zfill(3)}000"
        cs = (sum(ord(c) for c in pay) & 0xFF) ^ 0xFF
        pk = f"{pay}{str(cs).zfill(3)}\r"
        threading.Thread(target=self.safe_comm, args=(PORT_BOARD_1, pk.encode('ascii'), 0), daemon=True).start()

    def send_b2_gas_cmd(self):
        # Swapped Mapping: Valve slider -> Valve hardware | Pump slider -> Pump hardware
        v_t = int((self.gas_valve_pct.get()/100)*255) # Valve
        p_t = int((self.air_pump_pct.get()/100)*255) # Pump
       # pay = f"{str(p_t).zfill(3)}{str(v_t).zfill(3)}"  DV 14/04/26
        pay = f"{str(v_t).zfill(3)}{str(p_t).zfill(3)}"
        cs = (sum(ord(c) for c in pay) & 0xFF) ^ 0xFF
        pk = f"{pay}{str(cs).zfill(3)}\r"
        threading.Thread(target=self.safe_comm, args=(PORT_BOARD_2, pk.encode('ascii'), 20), daemon=True).start()

    def send_pump_cmd(self, rpm):
        # BLOCK ALL incoming commands if a recovery nudge or global stop is active
        if self.recovery_in_progress:
            return 
            
        self.pump_active = True if rpm > 0 else False
        p_body = struct.pack(">BBBBi", 1, 1, 0, 0, rpm)
        pk = p_body + struct.pack("B", sum(p_body) % 256)
        threading.Thread(target=self.safe_comm, args=(PORT_BLOOD_PUMP, pk, 9), daemon=True).start()

    # --- UI LAYOUT ---
    def create_layout(self):
        self.top_frame = tk.Frame(self.root, height=150); self.top_frame.pack(side=tk.TOP, fill=tk.X)
        led_c = tk.Frame(self.top_frame); led_c.pack(side=tk.LEFT, padx=20)
        self.err_led, self.err_circle = self.make_led(led_c, "Error", "red")
        self.run_led, self.run_circle = self.make_led(led_c, "Running", "gray")
        self.log_led, self.log_circle = self.make_led(led_c, "Rec", "gray")
        self.terminal = scrolledtext.ScrolledText(self.top_frame, height=7, width=105, font=('Courier', 9))
        self.terminal.pack(side=tk.LEFT, padx=10)
        
        mid = tk.Frame(self.root); mid.pack(fill=tk.BOTH, expand=True)
        self.db_frame = tk.Frame(mid, padx=10); self.db_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.sidebar = tk.Frame(mid, width=320, bg="#f4f4f4", padx=10); self.sidebar.pack(side=tk.RIGHT, fill=tk.Y)
        
        db = tk.LabelFrame(self.db_frame, text=" Perfusion Dashboard "); db.pack(fill=tk.X, pady=5)
        self.metrics = {}
        ly = [("PH", "pH", "black"), ("PO2", "pO2 kpa", "blue"), ("PCO2", "pCO2 kpa", "purple"),
              ("PRESS", "Pressure mmHg", "darkred"), ("FLOW", "Flow lpm", "darkgreen"), ("TEMP", "Temp C", "orange")]
        for i, (k, l, c) in enumerate(ly):
            f = tk.Frame(db, padx=25, pady=10); f.grid(row=i//3, column=i%3, sticky="w")
            tk.Label(f, text=l, font=('Arial', 10)).pack(anchor="w")
            lbl = tk.Label(f, text="--.--", font=('Arial', 32, 'bold'), fg=c); lbl.pack(anchor="w"); self.metrics[k] = lbl

        gf = tk.LabelFrame(self.db_frame, text=" Flow Trend "); gf.pack(fill=tk.BOTH, expand=True, pady=10)
        self.fig, self.ax = plt.subplots(figsize=(8, 3), dpi=90); self.fig.patch.set_facecolor('#f4f4f4')
        self.canvas = FigureCanvasTkAgg(self.fig, master=gf); self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        bp = tk.LabelFrame(self.sidebar, text=" Blood Pump (mmHg) "); bp.pack(fill=tk.X, pady=5)
        self.press_chk = tk.Checkbutton(bp, text="ENABLE AUTO PRESSURE", variable=self.auto_mode, font=('Arial', 9, 'bold'))
        self.press_chk.pack()
        tk.Label(bp, text="Target mmHg:").pack(side=tk.LEFT); tk.Entry(bp, textvariable=self.press_setpoint, width=5).pack(side=tk.LEFT)
        tk.Button(bp, text="SET MANUAL RPM", bg="green", fg="white", command=lambda: self.send_pump_cmd(int(self.rpm_ent.get()))).pack(fill=tk.X, pady=2)
        self.rpm_ent = tk.Entry(bp, justify='center'); self.rpm_ent.insert(0, "1000"); self.rpm_ent.pack()
        self.rpm_actual_lbl = tk.Label(bp, text="Actual: 0 RPM", font=('Arial', 10, 'bold')); self.rpm_actual_lbl.pack()

        b1 = tk.LabelFrame(self.sidebar, text=" Temperature (Celsius) "); b1.pack(fill=tk.X, pady=5)
        self.temp_chk = tk.Checkbutton(b1, text="ENABLE AUTO TEMP", variable=self.temp_auto_mode, font=('Arial', 9, 'bold'))
        self.temp_chk.pack()
        tk.Label(b1, text="Target Temp (18-40C):").pack()
        tk.Scale(b1, from_=18, to=40, resolution=0.1, orient=tk.HORIZONTAL, variable=self.temp_setpoint).pack(fill=tk.X)
        tk.Label(b1, text="Heater PWM (Indicator):").pack()
        tk.Scale(b1, from_=0, to=240, orient=tk.HORIZONTAL, variable=self.heater_pwm, state="disabled").pack(fill=tk.X)

        bg = tk.LabelFrame(self.sidebar, text=" Gas Control "); bg.pack(fill=tk.X, pady=5)
        tk.Scale(bg, from_=0, to=100, label="Air Pump Speed %", orient=tk.HORIZONTAL, variable=self.air_pump_pct).pack(fill=tk.X)
        tk.Scale(bg, from_=0, to=100, label="Gas Valve Duty %", orient=tk.HORIZONTAL, variable=self.gas_valve_pct).pack(fill=tk.X)
        tk.Button(bg, text="UPDATE GAS HARDWARE", bg="purple", fg="white", command=self.send_b2_gas_cmd).pack(fill=tk.X, pady=5)

        inf = tk.LabelFrame(self.sidebar, text=" Syringe Infusion "); inf.pack(fill=tk.X, pady=5)
        self.create_inf_row(inf, "Upper", PORT_UPPER_SYRINGE); self.create_inf_row(inf, "Lower", PORT_LOWER_SYRINGE)
        
        tk.Button(self.sidebar, text="STOP ALL", bg="red", fg="white", command=self.global_emergency_stop).pack(fill=tk.X, pady=10)
        self.btn_log = tk.Button(self.sidebar, text="Start Recording", command=self.toggle_logging); self.btn_log.pack(fill=tk.X)

    # --- UI & LOGGING ---
    def refresh_ui_labels(self):
        if not self.root.winfo_exists(): return # Stop if window is closed
        try:
            self.metrics["TEMP"].config(text=self.temp_val); self.metrics["PRESS"].config(text=self.press_val)
            self.metrics["PH"].config(text=self.ph_val); self.metrics["FLOW"].config(text=self.flow_val)
            self.rpm_actual_lbl.config(text=f"Actual: {self.actual_rpm} RPM")
            
            self.press_chk.config(fg="red" if self.auto_mode.get() else "black")
            self.temp_chk.config(fg="orange" if self.temp_auto_mode.get() else "black")
            
            if self.log_counter % 20 == 0: self.update_flow_graph()
            self.log_counter += 1
            self.log_led.itemconfig(self.log_circle, fill="blue" if self.is_logging else "gray")
        except: pass
        self.root.after(500, self.refresh_ui_labels)

    def update_flow_graph(self):
        try:
            val = float(self.flow_val)
            self.flow_history.append(val); self.time_history.append(datetime.now().strftime("%H:%M"))
            if len(self.flow_history) > self.max_graph_points: self.flow_history.pop(0); self.time_history.pop(0)
            self.ax.clear()
            self.ax.plot(self.time_history, self.flow_history, color='green', linewidth=1.5)
            self.ax.set_xticks(self.time_history[::48]); self.canvas.draw()
        except: pass

    # --- HELPERS ---
    def blood_pump_loop(self):
        stall_counter = 0
        recovery_attempted = False
        
        while True:
            try:
                if not self.root.winfo_exists(): break 
                
                # 1. Fetch Actual RPM
                p_body = struct.pack(">BBBBi", 1, 6, 3, 0, 0)
                reply = self.safe_comm(PORT_BLOOD_PUMP, p_body + struct.pack("B", sum(p_body)%256), 9)
                
                if reply and len(reply) == 9: 
                    self.actual_rpm = abs(struct.unpack(">i", reply[4:8])[0])
                    self.health_counts["BloodPump"] += 1
                    
                    # 2. Refined "Should Be Running" Logic
                    # Monitor stall if Manual is active OR Auto Mode is toggled ON
                    is_user_requesting_run = self.pump_active or self.auto_mode.get()
                    
                    if is_user_requesting_run and self.actual_rpm < 10:
                        stall_counter += 0.5 
                    else:
                        stall_counter = 0
                        self.motor_stalled = False
                        recovery_attempted = False 

                    # 3. REVERSE NUDGE (Triggers at 4 seconds)
                    if stall_counter == 4.0 and not recovery_attempted and not self.recovery_in_progress:
                        self.recovery_in_progress = True # SECURE THE BUS
                        self.log_msg("STALL DETECTED (AUTO/MANUAL): Attempting Recovery...")
                        
                        # Step A: Immediate Stop to clear current
                        self.safe_comm(PORT_BLOOD_PUMP, struct.pack(">BBBBiB", 1, 3, 0, 0, 0, 4), 9)
                        threading.Event().wait(0.4)
                        
                        # Step B: Stronger Reverse Nudge (ROL)
                        rev_pk = struct.pack(">BBBBi", 1, 2, 0, 0, 800)
                        self.safe_comm(PORT_BLOOD_PUMP, rev_pk + struct.pack("B", sum(rev_pk)%256), 9)
                        threading.Event().wait(0.6)
                        
                        # Step C: Re-fetch target and resume
                        self.recovery_in_progress = False # RELEASE BUS
                        try:
                            target = int(self.rpm_ent.get()) if not self.auto_mode.get() else self.press_pid.setpoint
                            self.send_pump_cmd(int(target))
                        except: pass
                        recovery_attempted = True

                    # 4. FINAL FAIL-SAFE
                    if stall_counter >= 10.0:
                        self.motor_stalled = True
                        self.log_msg("CRITICAL: Auto-Mode Stall Recovery Failed. Emergency Stop.")
                        self.global_emergency_stop() 
                        stall_counter = 0
            except: break
            threading.Event().wait(0.5)

    def parse_board_one(self, l):
        if "A," in l:
            self.health_counts["Board1"] += 1
            self.last_b1_receive_time = datetime.now() # Update on successful receive
            try:
                p = l.split(',')
                self.press_val, self.flow_val = f"{float(p[4]):.2f}", f"{float(p[6]):.2f}"
            except: pass

    def parse_terumo(self, l):
        f = [x.strip() for x in l.split('\t') if x.strip()]
        if len(f) >= 5 and re.search(r'\d{2}:\d{2}:\d{2}', f[0]):
            self.health_counts["Terumo"] += 1; self.terumo_active = True
            self.last_terumo_packet_time = datetime.now()
            self.ph_val, self.pco2_val, self.po2_val, self.temp_val = f[1], f[2], f[3], f[4]

    def log_msg(self, m):
        def a(): self.terminal.insert(tk.END, f"[{datetime.now().strftime('%H:%M:%S')}] {m}\n"); self.terminal.see(tk.END)
        self.root.after(0, a)

    def check_heartbeat_status(self):
        if not self.root.winfo_exists(): return 
        
        # Now checks if we haven't RECEIVED data for 5 seconds
        t_err = (datetime.now() - self.last_terumo_packet_time).total_seconds() > 15
        b1_err = (datetime.now() - self.last_b1_receive_time).total_seconds() > 5.0
        p_err = not self.port_status["Pump"]
        
        has_error = t_err or b1_err or p_err or self.motor_stalled or self.motor_overheat
        # Interlock logic: Running LED only green if we have live data from all sources
        all_ok = (not t_err) and (not b1_err) and (not p_err) and (not self.motor_stalled)
        
        self.run_led.itemconfig(self.run_circle, fill="green" if all_ok else "gray")
        self.err_led.itemconfig(self.err_circle, fill="red" if has_error else "gray")
        
        self.root.after(1000, self.check_heartbeat_status)

    def on_closing(self):
        """Polite shutdown sequence for UI exit."""
        try:
            self.log_msg("System shutting down. Entering safe state...")
            # Trigger emergency stop to kill heaters/pumps
            self.global_emergency_stop()
            # Brief wait for packets to clear
            threading.Event().wait(0.5)
        finally:
            self.root.destroy()
            os._exit(0) # Force exit all daemon threads
       
    def global_emergency_stop(self):
        # 1. PRIORITY LOCK: Stop all background pump loops immediately
        self.recovery_in_progress = True 
        self.pump_active = False  # Resets stall detection logic
        
        self.auto_mode.set(False)
        self.temp_auto_mode.set(False)
        self.heater_pwm.set(0)
        self.send_b1_cmd()
        
        # Immediate Hardware Stop (Instruction 3)
        stop_pk = struct.pack(">BBBBiB", 1, 3, 0, 0, 0, 4)
        self.safe_comm(PORT_BLOOD_PUMP, stop_pk, 9)
        
        self.air_pump_pct.set(0); self.gas_valve_pct.set(0); self.send_b2_gas_cmd()
        for p in [PORT_UPPER_SYRINGE, PORT_LOWER_SYRINGE]:
            self.syringe_pump_action(p, "0", "STOP")
        
        self.log_msg("GLOBAL STOP: All actuators de-energized.")
        # Release lock after 1.5s to clear old buffers
        self.root.after(1500, lambda: setattr(self, 'recovery_in_progress', False))

    def make_led(self, parent, text, color):
        f = tk.Frame(parent); f.pack(side=tk.LEFT, padx=5)
        tk.Label(f, text=text, font=('Arial', 8)).pack()
        c = tk.Canvas(f, width=25, height=25); c.pack()
        circ = c.create_oval(4, 4, 21, 21, fill=color, outline="black")
        return c, circ

    def create_inf_row(self, parent, name, port):
        f = tk.Frame(parent); f.pack(fill=tk.X)
        tk.Label(f, text=name).pack(side=tk.LEFT)
        ent = tk.Entry(f, width=5); ent.insert(0, "5.0"); ent.pack(side=tk.LEFT)
        tk.Button(f, text="RUN", command=lambda: self.syringe_pump_action(port, ent.get(), "RUN")).pack(side=tk.LEFT)
        tk.Button(f, text="STOP", command=lambda: self.syringe_pump_action(port, ent.get(), "STOP")).pack(side=tk.LEFT)

    def syringe_pump_action(self, port, rate, act):
        # State tracking for watchdog and recovery
        self.syringe_states[port] = act
        self.syringe_rates[port] = rate
        
        def task():
            # Standard command sequence for syringe drivers
            cmds = [f"DIA {SYRINGE_DIA}\r", f"RAT {rate} MH\r", "CLD INF\r", "CLT\r", "DIR INF\r", "RUN\r"] if act == "RUN" else ["STP\r"]
            for c in cmds: 
                self.safe_comm(port, c.encode('ascii'), 0)
                threading.Event().wait(0.15)
        
        threading.Thread(target=task, daemon=True).start()

    def start_health_monitor_thread(self):
        while True:
            threading.Event().wait(300.0)
            
            # Determine connection status for terminal report
            p_st = "OK" if self.port_status["Pump"] else "ERROR"
            b1_st = "OK" if self.health_counts["Board1"] > 0 else "LOST"
            t_st = "OK" if self.health_counts["Terumo"] > 0 else "LOST"
            
            # Construct diagnostic message
            msg = f"\n--- 5-MINUTE HEALTH PULSE ---\n"
            msg += f"CONN: Pump:{p_st} | Board1:{b1_st} | Terumo:{t_st}\n"
            
                # Report PID performance if active
            if self.temp_auto_mode.get():
                msg += f"TEMP PID: Target:{self.temp_setpoint.get()}C | Actual:{self.temp_val}C | PWM:{self.heater_pwm.get()}\n"
            if self.auto_mode.get():
                msg += f"PRESS PID: Target:{self.press_setpoint.get()}mmHg | Actual:{self.press_val}mmHg | RPM:{self.actual_rpm}\n"
            
            # Send to UI Terminal
            self.log_msg(msg)
            
            # Reset counters for next 5-minute window
            self.health_counts = {"Terumo":0, "Board1":0, "BloodPump":0}

    def start_syringe_watchdog_thread(self):
        while True:
            threading.Event().wait(60.0)  # Check every minute
            for port, state in self.syringe_states.items():
                if state == "RUN":
                    self.check_recov(port)

    def check_recov(self, port):
        """Restored helper to verify pump status and trigger recovery."""
        try:
            # Query pump status
            reply = self.safe_comm(port, b"\r", 0)
            if reply and b"S" in reply:  # 'S' indicates the pump has stopped
                self.log_msg(f"Watchdog: Unexpected stop detected. Restarting Syringe on Port {port}")
                self.syringe_pump_action(port, self.syringe_rates[port], "RUN")
        except:
            pass

    def board_one_listener(self):
        while True:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(5.0); s.connect((ES_IP, PORT_BOARD_1)); buf = ""
                    while True:
                        d = s.recv(1024).decode('ascii', errors='ignore')
                        if not d: break
                        buf += d
                        while "\r" in buf: line, buf = buf.split("\r", 1); self.parse_board_one(line.strip())
            except: threading.Event().wait(2.0)

    def terumo_listener(self):
        while True:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(10.0); s.connect((ES_IP, PORT_TERUMO)); buf = ""
                    while True:
                        d = s.recv(2048).decode('latin-1', errors='ignore')
                        if not d: break
                        buf += d
                        while "\r" in buf: line, buf = buf.split("\r", 1); self.parse_terumo(line)
            except: threading.Event().wait(2.0)

    def toggle_logging(self):
        if not self.is_logging:
            path = filedialog.asksaveasfilename(defaultextension=".csv")
            if path:
                self.log_filepath = path
                with open(path, 'w', newline='') as f:
                    csv.writer(f).writerow(["Time", "pH", "pCO2", "pO2", "Temp", "RPM", "Pressure", "Flow"])
                self.is_logging = True; self.btn_log.config(text="STOP RECORDING", bg="blue", fg="white")
        else: self.is_logging = False; self.btn_log.config(text="Start Recording", bg="#f0f0f0", fg="black")

if __name__ == "__main__":
    import time
    # MANDATORY: Allow ES-279/Network hardware 15 seconds to 
    # fully initialize before the UI tries to open the sockets.
    time.sleep(15) 
    
    root = tk.Tk()
    app = ClinicalConsole(root)
    # Ensure polite closure when the 'X' is clicked
    root.protocol("WM_DELETE_WINDOW", app.on_closing)
    root.mainloop()
