# --- VERSION 3.2.2 ---
# 1. HARDWARE TEST: Increased Heater PWM ceiling to 240 to overcome 35C plateau.
# 2. SAFETY: Heater forced to 0 if Pump communication fails (Interlock).
# 3. SAFETY: Pump maintains last RPM if Board 1 fails (Fail-Last-Setting).
# 4. DIAGNOSTIC: 5-minute Terminal Health Pulse restored.
# 5. TUNING: Kp=15, Ki=0.5, Windup=2000 for Thermal PID.

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
    def __init__(self, kp, ki, kd, setpoint, output_limits=(None, None), windup_limit=500):
        self.kp, self.ki, self.kd = kp, ki, kd
        self.setpoint = setpoint
        self.integral = 0
        self.last_error = 0
        self.min_out, self.max_out = output_limits
        self.windup_limit = windup_limit

    def update(self, measurement, dt=1.0):
        error = self.setpoint - measurement
        # Anti-windup using instance-specific limit
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
        self.root.title("Kidney Device Console v3.2.2")
        self.root.geometry("1450x980")
        
        # --- UI Data State ---
        self.ph_val = "--.--"; self.po2_val = "--.--"; self.pco2_val = "--.--"
        self.temp_val = "0.00"; self.press_val = "0.00"; self.flow_val = "0.00"
        self.actual_rpm = 0
        
        # Health Tracking Flags
        self.port_status = {"Pump": True, "Terumo": True, "Board1": True}
        self.health_counts = {"Terumo": 0, "Board1": 0, "BloodPump": 0}
        
        # PID Controllers
        self.auto_mode = tk.BooleanVar(value=False)
        self.press_setpoint = tk.DoubleVar(value=60.0)
        self.press_pid = PID(kp=1.5, ki=0.05, kd=0.2, setpoint=60.0, windup_limit=500)
        
        self.temp_auto_mode = tk.BooleanVar(value=False)
        self.temp_setpoint = tk.DoubleVar(value=37.0)
        # Ceiling increased to 240 for hardware test
        self.temp_pid = PID(kp=15.0, ki=0.5, kd=1.0, setpoint=37.0, output_limits=(0, 240), windup_limit=2000)
        
        self.last_b1_send_time = datetime.now()
        self.last_terumo_packet_time = datetime.now()
        
        # Hardware/Graphing State
        self.flow_history = []; self.time_history = []
        self.max_graph_points = 288 
        self.air_pump_pct = tk.IntVar(value=0); self.gas_valve_pct = tk.IntVar(value=0)
        self.air_valve = tk.BooleanVar(value=False); self.heat_dir = tk.BooleanVar(value=True) 
        self.heater_pwm = tk.IntVar(value=0); self.o2_pwm = tk.IntVar(value=0)

        self.terumo_active = False; self.is_logging = False; self.log_counter = 0 
        self.syringe_states = {PORT_UPPER_SYRINGE: "STOP", PORT_LOWER_SYRINGE: "STOP"}
        self.syringe_rates = {PORT_UPPER_SYRINGE: "5.0", PORT_LOWER_SYRINGE: "5.0"}
        
        self.cmd_lock = threading.Lock()
        self.pulse_data = {"pH": [], "Flow": []}

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
            # 1. Pressure PID
            b1_age = (datetime.now() - self.last_b1_send_time).total_seconds()
            if self.auto_mode.get() and b1_age < 5.0:
                try:
                    p_adj = self.press_pid.update(float(self.press_val))
                    new_rpm = max(min(self.actual_rpm + int(p_adj), 3500), 0)
                    if abs(p_adj) > 2:
                        self.send_pump_cmd(new_rpm)
                except: pass

            # 2. Temperature PID
            # SAFETY: Heater interlock with Pump health and RPM
            if self.temp_auto_mode.get() and self.port_status["Pump"] and self.actual_rpm > 150:
                try:
                    t_out = self.temp_pid.update(float(self.temp_val))
                    self.heater_pwm.set(int(t_out))
                except: pass
            else:
                self.heater_pwm.set(0)

            # 3. Watchdog Pulse (Mandatory every 2s)
            if (datetime.now() - self.last_b1_send_time).total_seconds() > 2.0:
                self.send_b1_cmd()
            
            threading.Event().wait(1.0)

    # --- DATA PARSING ---
    def parse_board_one(self, l):
        if "A," in l:
            self.health_counts["Board1"] += 1
            try:
                parts = l.split(',')
                # Confirmed Indices: Pressure [4], Flow [6]
                self.press_val = f"{float(parts[4]):.2f}"
                self.flow_val = f"{float(parts[6]):.2f}"
            except: pass

    def parse_terumo(self, l):
        f = [x.strip() for x in l.split('\t') if x.strip()]
        if len(f) >= 5 and re.search(r'\d{2}:\d{2}:\d{2}', f[0]):
            self.health_counts["Terumo"] += 1; self.terumo_active = True
            self.last_terumo_packet_time = datetime.now()
            self.ph_val, self.pco2_val, self.po2_val, self.temp_val = f[1], f[2], f[3], f[4]

    # --- COMMUNICATIONS ---
    def safe_comm(self, port, packet, expected_len):
        acquired = self.cmd_lock.acquire(timeout=2.0)
        if not acquired: return None 
        res = None
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1.2); s.connect((ES_IP, port)); s.sendall(packet)
                if expected_len > 0: res = s.recv(expected_len)
                else: res = s.recv(1024)
            if port == PORT_BLOOD_PUMP: self.port_status["Pump"] = True
            if port == PORT_BOARD_1: self.port_status["Board1"] = True
        except:
            if port == PORT_BLOOD_PUMP: self.port_status["Pump"] = False
            if port == PORT_BOARD_1: self.port_status["Board1"] = False
        finally: self.cmd_lock.release()
        return res

    def send_pump_cmd(self, rpm):
        p_body = struct.pack(">BBBBi", 1, 1, 0, 0, rpm)
        pk = p_body + struct.pack("B", sum(p_body) % 256)
        threading.Thread(target=self.safe_comm, args=(PORT_BLOOD_PUMP, pk, 9), daemon=True).start()

    def send_b1_cmd(self):
        self.last_b1_send_time = datetime.now()
        g = 0x80 if self.air_valve.get() else 0
        if self.heat_dir.get(): g |= 0x08
        pay = f"{str(g).zfill(3)}{str(self.heater_pwm.get()).zfill(3)}{str(self.o2_pwm.get()).zfill(3)}"
        cs = (sum(ord(c) for c in pay) & 0xFF) ^ 0xFF
        pk = f"{pay}{str(cs).zfill(3)}\r"
        threading.Thread(target=self.safe_comm, args=(PORT_BOARD_1, pk.encode('ascii'), 0), daemon=True).start()

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

        gf = tk.LabelFrame(self.db_frame, text=" 24-Hour Flow Trend "); gf.pack(fill=tk.BOTH, expand=True, pady=10)
        self.fig, self.ax = plt.subplots(figsize=(8, 3), dpi=90); self.fig.patch.set_facecolor('#f4f4f4')
        self.canvas = FigureCanvasTkAgg(self.fig, master=gf); self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        # Blood Pump Sidebar
        bp = tk.LabelFrame(self.sidebar, text=" Blood Pump (Auto-Pressure) "); bp.pack(fill=tk.X, pady=5)
        tk.Checkbutton(bp, text="ENABLE AUTO PRESSURE", variable=self.auto_mode, fg="darkred").pack()
        tk.Label(bp, text="Target mmHg:").pack(side=tk.LEFT); tk.Entry(bp, textvariable=self.press_setpoint, width=5).pack(side=tk.LEFT)
        tk.Button(bp, text="SET RPM", bg="green", fg="white", command=lambda: self.send_pump_cmd(int(self.rpm_ent.get()))).pack(fill=tk.X, pady=2)
        self.rpm_ent = tk.Entry(bp, justify='center'); self.rpm_ent.insert(0, "1000"); self.rpm_ent.pack()
        self.rpm_actual_lbl = tk.Label(bp, text="Actual: 0 RPM", font=('Arial', 10, 'bold')); self.rpm_actual_lbl.pack()

        # Thermal Sidebar (Limit 240)
        b1 = tk.LabelFrame(self.sidebar, text=" Thermal Control (Board 1) "); b1.pack(fill=tk.X, pady=5)
        tk.Checkbutton(b1, text="ENABLE AUTO TEMP", variable=self.temp_auto_mode, fg="orange").pack()
        tk.Label(b1, text="Target Temp (18-40C):").pack()
        tk.Scale(b1, from_=18, to=40, resolution=0.1, orient=tk.HORIZONTAL, variable=self.temp_setpoint).pack(fill=tk.X)
        tk.Label(b1, text="Man. Heater PWM (0-240):").pack()
        tk.Scale(b1, from_=0, to=240, orient=tk.HORIZONTAL, variable=self.heater_pwm).pack(fill=tk.X)
        tk.Checkbutton(b1, text="Air Valve", variable=self.air_valve).pack(anchor="w")
        tk.Checkbutton(b1, text="Heat Mode", variable=self.heat_dir).pack(anchor="w")
        tk.Button(b1, text="UPDATE BOARD 1", bg="blue", fg="white", command=self.send_b1_cmd).pack(fill=tk.X, pady=5)

        bg = tk.LabelFrame(self.sidebar, text=" Gas Control (Board 2) "); bg.pack(fill=tk.X, pady=5)
        tk.Scale(bg, from_=0, to=100, label="Gas Valve %", orient=tk.HORIZONTAL, variable=self.gas_valve_pct).pack(fill=tk.X)
        tk.Scale(bg, from_=0, to=100, label="Air Pump %", orient=tk.HORIZONTAL, variable=self.air_pump_pct).pack(fill=tk.X)
        tk.Button(bg, text="UPDATE GAS", bg="purple", fg="white", command=self.send_b2_gas_cmd).pack(fill=tk.X, pady=5)

        inf = tk.LabelFrame(self.sidebar, text=" Infusion "); inf.pack(fill=tk.X, pady=5)
        self.create_inf_row(inf, "Upper", PORT_UPPER_SYRINGE); self.create_inf_row(inf, "Lower", PORT_LOWER_SYRINGE)
        
        tk.Button(self.sidebar, text="GLOBAL STOP", bg="red", fg="white", font=('Arial', 12, 'bold'), command=self.global_emergency_stop).pack(fill=tk.X, pady=10)
        self.btn_log = tk.Button(self.sidebar, text="Start Recording", command=self.toggle_logging); self.btn_log.pack(fill=tk.X)

    # --- MONITORING TOOLS ---
    def check_heartbeat_status(self):
        t_err = (datetime.now() - self.last_terumo_packet_time).total_seconds() > 15
        b1_err = (datetime.now() - self.last_b1_send_time).total_seconds() > 5.0
        p_err = not self.port_status["Pump"]
        all_ok = (not t_err) and (not b1_err) and (not p_err)
        self.run_led.itemconfig(self.run_circle, fill="green" if all_ok else "gray")
        self.err_led.itemconfig(self.err_circle, fill="red" if not all_ok else "gray")
        self.root.after(1000, self.check_heartbeat_status)

    def start_health_monitor_thread(self):
        while True:
            threading.Event().wait(300.0) # 5 Min Pulse
            p_st = "OK" if self.port_status["Pump"] else "ERROR"
            t_st = "OK" if self.health_counts["Terumo"] > 0 else "LOST"
            b1_st = "OK" if self.health_counts["Board1"] > 0 else "LOST"
            msg = f"\n--- 5-MIN HEALTH PULSE ---\nCONN: Pump:{p_st} | Terumo:{t_st} | Board1:{b1_st}\n"
            if self.temp_auto_mode.get():
                msg += f"TEMP PID: Target:{self.temp_setpoint.get()}C | Actual:{self.temp_val}C | PWM:{self.heater_pwm.get()}\n"
            self.log_msg(msg)
            self.health_counts = {"Terumo":0, "Board1":0, "BloodPump":0}

    # --- SUPPORTING METHODS ---
    def log_msg(self, m):
        def a():
            self.terminal.insert(tk.END, f"[{datetime.now().strftime('%H:%M:%S')}] {m}\n")
            self.terminal.see(tk.END)
        self.root.after(0, a)

    def blood_pump_loop(self):
        while True:
            p_body = struct.pack(">BBBBi", 1, 6, 3, 0, 0)
            pk = p_body + struct.pack("B", sum(p_body) % 256)
            reply = self.safe_comm(PORT_BLOOD_PUMP, pk, 9)
            if reply and len(reply) == 9: self.actual_rpm = abs(struct.unpack(">i", reply[4:8])[0])
            threading.Event().wait(0.5)

    def refresh_ui_labels(self):
        try:
            self.metrics["PH"].config(text=self.ph_val); self.metrics["PO2"].config(text=self.po2_val)
            self.metrics["PCO2"].config(text=self.pco2_val); self.metrics["TEMP"].config(text=self.temp_val)
            self.metrics["PRESS"].config(text=self.press_val); self.metrics["FLOW"].config(text=self.flow_val)
            self.rpm_actual_lbl.config(text=f"Actual: {self.actual_rpm} RPM")
            if self.is_logging and self.log_counter % 2 == 0:
                with open(self.log_filepath, 'a', newline='') as f:
                    csv.writer(f).writerow([datetime.now().strftime("%H:%M:%S"), self.ph_val, self.pco2_val, self.po2_val, self.temp_val, self.actual_rpm, self.press_val, self.flow_val])
            self.log_counter += 1
            self.log_led.itemconfig(self.log_circle, fill="blue" if self.is_logging else "gray")
        except: pass
        self.root.after(500, self.refresh_ui_labels)

    def global_emergency_stop(self):
        self.auto_mode.set(False); self.temp_auto_mode.set(False)
        self.heater_pwm.set(0); self.send_b1_cmd()
        self.send_pump_cmd(0)
        self.air_pump_pct.set(0); self.gas_valve_pct.set(0); self.send_b2_gas_cmd()
        self.syringe_pump_action(PORT_UPPER_SYRINGE, "0", "STOP")
        self.syringe_pump_action(PORT_LOWER_SYRINGE, "0", "STOP")

    def make_led(self, parent, text, color):
        f = tk.Frame(parent); f.pack(side=tk.LEFT, padx=5)
        tk.Label(f, text=text, font=('Arial', 8, 'bold')).pack()
        c = tk.Canvas(f, width=25, height=25); c.pack()
        circ = c.create_oval(4, 4, 21, 21, fill=color, outline="black")
        return c, circ

    def create_inf_row(self, parent, name, port):
        f = tk.Frame(parent, pady=2); f.pack(fill=tk.X)
        tk.Label(f, text=f"{name}:", font=('Arial', 8)).pack(side=tk.LEFT)
        ent = tk.Entry(f, width=5); ent.insert(0, "5.0"); ent.pack(side=tk.LEFT, padx=2)
        var = tk.StringVar(value="STOP")
        tk.Radiobutton(f, text="On", variable=var, value="RUN", command=lambda: self.syringe_pump_action(port, ent.get(), "RUN")).pack(side=tk.LEFT)
        tk.Radiobutton(f, text="Off", variable=var, value="STOP", command=lambda: self.syringe_pump_action(port, ent.get(), "STOP")).pack(side=tk.LEFT)

    def syringe_pump_action(self, port, rate, act):
        self.syringe_states[port] = act
        def task():
            cmds = [f"DIA {SYRINGE_DIA}\r", f"RAT {rate} MH\r", "CLD INF\r", "CLT\r", "DIR INF\r", "RUN\r"] if act == "RUN" else ["STP\r"]
            for c in cmds: self.safe_comm(port, c.encode('ascii'), 0); threading.Event().wait(0.15)
        threading.Thread(target=task, daemon=True).start()

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
            except: self.terumo_active = False; threading.Event().wait(2.0)

    def toggle_logging(self):
        if not self.is_logging:
            path = filedialog.asksaveasfilename(defaultextension=".csv")
            if path:
                self.log_filepath = path
                with open(path, 'w', newline='') as f:
                    csv.writer(f).writerow(["Time", "pH", "pCO2", "pO2", "Temp", "RPM", "Pressure", "Flow"])
                self.is_logging = True; self.btn_log.config(text="STOP RECORDING", bg="blue", fg="white")
        else: self.is_logging = False; self.btn_log.config(text="Start Recording", bg="#f0f0f0", fg="black")

    def send_b2_gas_cmd(self):
        v_t, p_t = int((self.gas_valve_pct.get()/100)*255), int((self.air_pump_pct.get()/100)*255)
        pay = f"{str(p_t).zfill(3)}{str(v_t).zfill(3)}"
        cs = (sum(ord(c) for c in pay) & 0xFF) ^ 0xFF
        pk = f"{pay}{str(cs).zfill(3)}\r"
        threading.Thread(target=self.safe_comm, args=(PORT_BOARD_2, pk.encode('ascii'), 20), daemon=True).start()

    def start_syringe_watchdog_thread(self):
        while True:
            threading.Event().wait(60.0)
            for p, st in self.syringe_states.items():
                if st == "RUN": self.check_recov(p)

    def check_recov(self, p):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1.0); s.connect((ES_IP, p)); s.sendall(b"\r")
                if "S" in s.recv(1024).decode('ascii'): self.syringe_pump_action(p, "5.0", "RUN")
        except: pass

    def update_flow_graph(self):
        try:
            self.flow_history.append(float(self.flow_val)); self.time_history.append(datetime.now().strftime("%H:%M"))
            if len(self.flow_history) > self.max_graph_points: self.flow_history.pop(0); self.time_history.pop(0)
            self.ax.clear(); self.ax.plot(self.time_history, self.flow_history, color='green')
            self.ax.set_xticks(self.time_history[::48]); self.canvas.draw()
        except: pass

if __name__ == "__main__":
    root = tk.Tk(); app = ClinicalConsole(root); root.mainloop()
