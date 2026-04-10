# --- VERSION 3.0.0-PID ---
# 1. FIXED: Board 1 indices set to [4] for Pressure and [6] for Flow.
# 2. NEW: PID Class for automated Pressure-to-RPM feedback.
# 3. NEW: UI Toggle for Manual/Auto modes and Setpoint entry.
# 4. LOGGING: Retains 1Hz cadence and 24hr Flow Trend.

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
SYRINGE_DIA = "29.70" 

class PID:
    def __init__(self, kp, ki, kd, setpoint):
        self.kp, self.ki, self.kd = kp, ki, kd
        self.setpoint = setpoint
        self.integral = 0
        self.last_error = 0

    def update(self, measurement, dt=1.0):
        error = self.setpoint - measurement
        # Anti-windup: limit integral contribution
        self.integral = max(min(self.integral + (error * dt), 500), -500)
        derivative = (error - self.last_error) / dt
        output = (self.kp * error) + (self.ki * self.integral) + (self.kd * derivative)
        self.last_error = error
        return output

class ClinicalConsole:
    def __init__(self, root):
        self.root = root
        self.root.title("Kidney Device Console v3.0.0-PID")
        self.root.geometry("1400x950")
        
        # --- UI Data State ---
        self.ph_val = "--.--"; self.po2_val = "--.--"; self.pco2_val = "--.--"
        self.temp_val = "--.--"; self.press_val = "0.00"; self.flow_val = "0.00"
        self.actual_rpm = 0
        
        # PID State
        self.auto_mode = tk.BooleanVar(value=False)
        self.pressure_setpoint = tk.DoubleVar(value=60.0)
        self.pid = PID(kp=1.5, ki=0.05, kd=0.2, setpoint=60.0)
        
        # Graphing State
        self.flow_history = []; self.time_history = []
        self.max_graph_points = 288 
        
        # Hardware States
        self.air_pump_pct = tk.IntVar(value=0); self.gas_valve_pct = tk.IntVar(value=0)
        self.air_valve = tk.BooleanVar(value=False); self.heat_dir = tk.BooleanVar(value=True) 
        self.heater_pwm = tk.IntVar(value=0); self.o2_pwm = tk.IntVar(value=0)

        self.terumo_active = False; self.is_logging = False; self.log_counter = 0 
        self.last_terumo_packet_time = datetime.now()
        
        self.syringe_states = {PORT_UPPER_SYRINGE: "STOP", PORT_LOWER_SYRINGE: "STOP"}
        self.syringe_rates = {PORT_UPPER_SYRINGE: "5.0", PORT_LOWER_SYRINGE: "5.0"}
        
        self.cmd_lock = threading.Lock()
        self.health_counts = {"Terumo": 0, "Board1": 0, "Board2": 0, "BloodPump": 0}
        self.pulse_data = {"pH": [], "Flow": []}

        self.create_layout()
        self.refresh_ui_labels()
        
        # Launch Threads
        threading.Thread(target=self.terumo_listener, daemon=True).start()
        threading.Thread(target=self.board_one_listener, daemon=True).start()
        threading.Thread(target=self.blood_pump_loop, daemon=True).start()
        threading.Thread(target=self.pid_control_loop, daemon=True).start()
        threading.Thread(target=self.start_health_monitor_thread, daemon=True).start()
        threading.Thread(target=self.start_syringe_watchdog_thread, daemon=True).start()
        self.check_heartbeat_status()

    # --- PID CONTROL LOOP ---
    def pid_control_loop(self):
        """1Hz loop to adjust RPM based on Pressure Index 4."""
        while True:
            if self.auto_mode.get():
                try:
                    curr_p = float(self.press_val)
                    self.pid.setpoint = self.pressure_setpoint.get()
                    
                    adjustment = self.pid.update(curr_p)
                    new_rpm = self.actual_rpm + int(adjustment)
                    
                    # Safety Clamping
                    new_rpm = max(min(new_rpm, 3500), 0)
                    
                    # Apply if change is required
                    if abs(adjustment) > 2:
                        p_body = struct.pack(">BBBBi", 1, 1, 0, 0, new_rpm)
                        packet = p_body + struct.pack("B", sum(p_body) % 256)
                        self.safe_comm(PORT_BLOOD_PUMP, packet, 9)
                except ValueError: pass
            
            threading.Event().wait(1.0)

    # --- UPDATED BOARD 1 PARSING ---
    def parse_board_one(self, l):
        if "A," in l:
            self.health_counts["Board1"] += 1
            try:
                parts = l.split(',')
                # Confirmed Indices: Pressure [4], Flow [6]
                p_v = float(parts[4])
                f_v = float(parts[6])
                
                self.press_val = f"{p_v:.2f}"
                self.flow_val = f"{f_v:.2f}"
                self.pulse_data["Flow"].append(f_v)
            except (IndexError, ValueError): pass

    # --- UI LAYOUT ---
    def create_layout(self):
        # Top LEDs / Terminal
        self.top_frame = tk.Frame(self.root, height=150); self.top_frame.pack(side=tk.TOP, fill=tk.X)
        led_c = tk.Frame(self.top_frame); led_c.pack(side=tk.LEFT, padx=20)
        self.err_led, self.err_circle = self.make_led(led_c, "Error", "red")
        self.run_led, self.run_circle = self.make_led(led_c, "Running", "gray")
        self.log_led, self.log_circle = self.make_led(led_c, "Rec", "gray")
        self.terminal = scrolledtext.ScrolledText(self.top_frame, height=7, width=100, font=('Courier', 9))
        self.terminal.pack(side=tk.LEFT, padx=10)
        
        mid = tk.Frame(self.root); mid.pack(fill=tk.BOTH, expand=True)
        self.db_frame = tk.Frame(mid, padx=10); self.db_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.sidebar = tk.Frame(mid, width=320, bg="#f4f4f4", padx=10); self.sidebar.pack(side=tk.RIGHT, fill=tk.Y)
        
        # Metrics
        db = tk.LabelFrame(self.db_frame, text=" Perfusion Dashboard "); db.pack(fill=tk.X, pady=5)
        self.metrics = {}
        ly = [("PH", "pH", "black"), ("PO2", "pO2 kpa", "blue"), ("PCO2", "pCO2 kpa", "purple"),
              ("PRESS", "Pressure mmHg", "darkred"), ("FLOW", "Flow lpm", "darkgreen"), ("TEMP", "Temp C", "orange")]
        for i, (k, l, c) in enumerate(ly):
            f = tk.Frame(db, padx=25, pady=10); f.grid(row=i//3, column=i%3, sticky="w")
            tk.Label(f, text=l, font=('Arial', 10)).pack(anchor="w")
            lbl = tk.Label(f, text="--.--", font=('Arial', 32, 'bold'), fg=c); lbl.pack(anchor="w"); self.metrics[k] = lbl

        # 24hr Flow Graph
        gf = tk.LabelFrame(self.db_frame, text=" 24-Hour Flow Trend "); gf.pack(fill=tk.BOTH, expand=True, pady=10)
        self.fig, self.ax = plt.subplots(figsize=(8, 3), dpi=90); self.fig.patch.set_facecolor('#f4f4f4')
        self.canvas = FigureCanvasTkAgg(self.fig, master=gf); self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        # Sidebar: Gas (B2)
        bg = tk.LabelFrame(self.sidebar, text=" Gas Control (B2) "); bg.pack(fill=tk.X, pady=5)
        tk.Label(bg, text="Gas Valve %:").pack(); tk.Scale(bg, from_=0, to=100, orient=tk.HORIZONTAL, variable=self.gas_valve_pct).pack(fill=tk.X)
        tk.Label(bg, text="Air Pump %:").pack(); tk.Scale(bg, from_=0, to=100, orient=tk.HORIZONTAL, variable=self.air_pump_pct).pack(fill=tk.X)
        tk.Button(bg, text="UPDATE GAS", bg="purple", fg="white", command=self.send_b2_gas_cmd).pack(fill=tk.X, pady=5)
        
        # Sidebar: Blood Pump + PID Controls
        bp = tk.LabelFrame(self.sidebar, text=" Blood Pump Control "); bp.pack(fill=tk.X, pady=5)
        tk.Checkbutton(bp, text="AUTO PRESSURE MODE", variable=self.auto_mode, fg="darkred", font=('Arial', 9, 'bold')).pack()
        tk.Label(bp, text="Target Pressure (mmHg):").pack()
        tk.Entry(bp, textvariable=self.pressure_setpoint, width=7, justify='center').pack()
        
        tk.Label(bp, text="Manual RPM Set:", pady=5).pack()
        self.rpm_ent = tk.Entry(bp, justify='center'); self.rpm_ent.insert(0, "1000"); self.rpm_ent.pack()
        tk.Button(bp, text="SET RPM (Manual)", bg="green", fg="white", command=self.blood_pump_go).pack(fill=tk.X, pady=5)
        self.rpm_actual_lbl = tk.Label(bp, text="Actual: 0 RPM", font=('Arial', 10, 'bold')); self.rpm_actual_lbl.pack()

        # Sidebar: Infusion
        inf = tk.LabelFrame(self.sidebar, text=" Infusion Control "); inf.pack(fill=tk.X, pady=5)
        self.create_inf_row(inf, "Upper Syringe", PORT_UPPER_SYRINGE); self.create_inf_row(inf, "Lower Syringe", PORT_LOWER_SYRINGE)

        # Sidebar: Thermal (B1)
        b1 = tk.LabelFrame(self.sidebar, text=" Board 1 (Thermal) "); b1.pack(fill=tk.X, pady=5)
        tk.Checkbutton(b1, text="Air Valve", variable=self.air_valve).pack(anchor="w")
        tk.Checkbutton(b1, text="Heat Mode", variable=self.heat_dir).pack(anchor="w")
        tk.Scale(b1, from_=0, to=180, orient=tk.HORIZONTAL, variable=self.heater_pwm).pack(fill=tk.X)
        tk.Button(b1, text="UPDATE BOARD 1", bg="blue", fg="white", command=self.send_b1_cmd).pack(fill=tk.X, pady=5)

        tk.Button(self.sidebar, text="GLOBAL STOP", bg="red", fg="white", font=('Arial', 12, 'bold'), command=self.global_emergency_stop).pack(fill=tk.X, pady=15)
        self.btn_log = tk.Button(self.sidebar, text="Start Recording", command=self.toggle_logging); self.btn_log.pack(fill=tk.X)

    # ... (Rest of safe_comm, terumo_listener, board_one_listener, syringe_pump_action, blood_pump_loop, toggle_logging, etc. from v2.9.3) ...

    def safe_comm(self, port, packet, expected_len):
        acquired = self.cmd_lock.acquire(timeout=2.0)
        if not acquired: return None 
        res = None
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1.2); s.connect((ES_IP, port)); s.sendall(packet)
                if expected_len > 0: res = s.recv(expected_len)
                else: res = s.recv(1024)
        except: pass
        finally: self.cmd_lock.release()
        return res

    def blood_pump_loop(self):
        while True:
            p_body = struct.pack(">BBBBi", 1, 6, 3, 0, 0)
            pk = p_body + struct.pack("B", sum(p_body) % 256)
            reply = self.safe_comm(PORT_BLOOD_PUMP, pk, 9)
            if reply and len(reply) == 9:
                self.actual_rpm = abs(struct.unpack(">i", reply[4:8])[0])
                self.health_counts["BloodPump"] += 1
            threading.Event().wait(0.5)

    def blood_pump_go(self):
        val = int(self.rpm_ent.get())
        p_body = struct.pack(">BBBBi", 1, 1, 0, 0, val)
        pk = p_body + struct.pack("B", sum(p_body) % 256)
        threading.Thread(target=self.safe_comm, args=(PORT_BLOOD_PUMP, pk, 9), daemon=True).start()

    def send_b1_cmd(self):
        g = 0x80 if self.air_valve.get() else 0
        if self.heat_dir.get(): g |= 0x08
        pay = f"{str(g).zfill(3)}{str(self.heater_pwm.get()).zfill(3)}{str(self.o2_pwm.get()).zfill(3)}"
        cs = (sum(ord(c) for c in pay) & 0xFF) ^ 0xFF
        pk = f"{pay}{str(cs).zfill(3)}\r"
        threading.Thread(target=self.safe_comm, args=(PORT_BOARD_1, pk.encode('ascii'), 0), daemon=True).start()

    def send_b2_gas_cmd(self):
        v_t = int((self.gas_valve_pct.get() / 100) * 255)
        p_t = int((self.air_pump_pct.get() / 100) * 255)
        pay = f"{str(p_t).zfill(3)}{str(v_t).zfill(3)}"
        cs = (sum(ord(c) for c in pay) & 0xFF) ^ 0xFF
        pk = f"{pay}{str(cs).zfill(3)}\r"
        threading.Thread(target=self.safe_comm, args=(PORT_BOARD_2, pk.encode('ascii'), 20), daemon=True).start()

    def syringe_pump_action(self, port, rate, act):
        self.syringe_states[port], self.syringe_rates[port] = act, rate
        def task():
            cmds = [f"DIA {SYRINGE_DIA}\r", f"RAT {rate} MH\r", "CLD INF\r", "CLT\r", "DIR INF\r", "RUN\r"] if act == "RUN" else ["STP\r"]
            for c in cmds:
                self.safe_comm(port, c.encode('ascii'), 0)
                threading.Event().wait(0.15)
        threading.Thread(target=task, daemon=True).start()

    def toggle_logging(self):
        if not self.is_logging:
            path = filedialog.asksaveasfilename(defaultextension=".csv")
            if path:
                self.log_filepath = path
                with open(path, 'w', newline='') as f:
                    csv.writer(f).writerow(["Time", "pH", "pCO2", "pO2", "Temp", "RPM", "Pressure", "Flow"])
                self.is_logging = True; self.btn_log.config(text="STOP RECORDING", bg="blue", fg="white")
        else: self.is_logging = False; self.btn_log.config(text="Start Recording", bg="#f0f0f0", fg="black")

    def refresh_ui_labels(self):
        try:
            self.metrics["PH"].config(text=self.ph_val); self.metrics["PO2"].config(text=self.po2_val)
            self.metrics["PCO2"].config(text=self.pco2_val); self.metrics["TEMP"].config(text=self.temp_val)
            self.metrics["PRESS"].config(text=self.press_val); self.metrics["FLOW"].config(text=self.flow_val)
            self.rpm_actual_lbl.config(text=f"Actual: {self.actual_rpm} RPM")
            self.log_counter += 1
            if self.log_counter >= 2:
                if self.is_logging and self.log_filepath:
                    with open(self.log_filepath, 'a', newline='') as f:
                        csv.writer(f).writerow([datetime.now().strftime("%H:%M:%S"), self.ph_val, self.pco2_val, self.po2_val, self.temp_val, self.actual_rpm, self.press_val, self.flow_val])
                self.log_counter = 0
            self.log_led.itemconfig(self.log_circle, fill="blue" if self.is_logging else "gray")
        except: pass
        self.root.after(500, self.refresh_ui_labels)

    def start_health_monitor_thread(self):
        while True:
            threading.Event().wait(300.0)
            self.root.after(0, self.update_flow_graph)
            self.health_counts = {"Terumo":0, "Board1":0, "Board2":0, "BloodPump":0}; self.pulse_data = {"pH":[], "Flow":[]}

    def update_flow_graph(self):
        try:
            self.flow_history.append(float(self.flow_val)); self.time_history.append(datetime.now().strftime("%H:%M"))
            if len(self.flow_history) > self.max_graph_points: self.flow_history.pop(0); self.time_history.pop(0)
            self.ax.clear(); self.ax.plot(self.time_history, self.flow_history, color='green')
            self.ax.set_xticks(self.time_history[::48]); self.canvas.draw()
        except: pass

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
            except: self.terumo_active = False; threading.Event().wait(2.0)

    def parse_terumo(self, l):
        f = [x.strip() for x in l.split('\t') if x.strip()]
        if len(f) >= 5 and re.search(r'\d{2}:\d{2}:\d{2}', f[0]):
            self.health_counts["Terumo"] += 1; self.terumo_active = True; self.last_terumo_packet_time = datetime.now()
            self.ph_val, self.pco2_val, self.po2_val, self.temp_val = f[1], f[2], f[3], f[4]

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

    def start_syringe_watchdog_thread(self):
        while True:
            threading.Event().wait(60.0)
            for p, st in self.syringe_states.items():
                if st == "RUN": self.check_recov(p)

    def check_recov(self, p):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1.0); s.connect((ES_IP, p)); s.sendall(b"\r")
                if "S" in s.recv(1024).decode('ascii'): self.syringe_pump_action(p, self.syringe_rates[p], "RUN")
        except: pass

    def global_emergency_stop(self):
        self.auto_mode.set(False); self.heater_pwm.set(0); self.air_valve.set(False); self.send_b1_cmd()
        self.air_pump_pct.set(0); self.gas_valve_pct.set(0); self.send_b2_gas_cmd()
        p_stop = struct.pack(">BBBBi", 1, 3, 0, 0, 0); pk = p_stop + struct.pack("B", sum(p_stop) % 256)
        threading.Thread(target=self.safe_comm, args=(PORT_BLOOD_PUMP, pk, 9), daemon=True).start()
        self.syringe_pump_action(PORT_UPPER_SYRINGE, "0", "STOP"); self.syringe_pump_action(PORT_LOWER_SYRINGE, "0", "STOP")

    def create_inf_row(self, parent, name, port):
        f = tk.Frame(parent, pady=2); f.pack(fill=tk.X)
        tk.Label(f, text=f"{name}:", font=('Arial', 8)).pack(side=tk.LEFT)
        ent = tk.Entry(f, width=5); ent.insert(0, "5.0"); ent.pack(side=tk.LEFT, padx=2)
        var = tk.StringVar(value="STOP")
        tk.Radiobutton(f, text="On", variable=var, value="RUN", command=lambda: self.syringe_pump_action(port, ent.get(), "RUN")).pack(side=tk.LEFT)
        tk.Radiobutton(f, text="Off", variable=var, value="STOP", command=lambda: self.syringe_pump_action(port, ent.get(), "STOP")).pack(side=tk.LEFT)

    def make_led(self, parent, text, color):
        f = tk.Frame(parent); f.pack(side=tk.LEFT, padx=5)
        tk.Label(f, text=text, font=('Arial', 8, 'bold')).pack()
        c = tk.Canvas(f, width=25, height=25); c.pack()
        circ = c.create_oval(4, 4, 21, 21, fill=color, outline="black")
        return c, circ

    def check_heartbeat_status(self):
        el = (datetime.now() - self.last_terumo_packet_time).total_seconds()
        is_r = el < 12 and self.terumo_active
        self.run_led.itemconfig(self.run_circle, fill="green" if is_r else "gray")
        self.err_led.itemconfig(self.err_circle, fill="red" if not is_r else "gray")
        self.root.after(1000, self.check_heartbeat_status)

if __name__ == "__main__":
    root = tk.Tk(); app = ClinicalConsole(root); root.mainloop()
