import sqlite3
import time
import json
import subprocess
import os
import hashlib
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

DB_FILE = 'rendersync.db'

# ==========================================
# 🔴 UniPush 1.0 (个推) 核心鉴权配置
# ==========================================
UNIPUSH_APP_ID = "XipgowGnaU7fbXbYVsQut5"
UNIPUSH_APP_KEY = "8zv3xcbj2JArMgYPQD0Ig3"
UNIPUSH_MASTER_SECRET = "37skbksceh7jIYlLFpRqc7"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    # 彻底解除 SQLite 的读写互斥锁，支持高并发
    c.execute('PRAGMA journal_mode=WAL;')
    
    # 渲染节点表
    c.execute('''
        CREATE TABLE IF NOT EXISTS render_nodes (
            machine_id TEXT PRIMARY KEY,
            perm_key TEXT,
            temp_key TEXT,
            expire_timestamp REAL,
            project TEXT,
            status TEXT,
            render_time TEXT,
            last_update REAL
        )
    ''')
    
    # 动态增加字段（容错机制）
    try: c.execute("ALTER TABLE render_nodes ADD COLUMN progress INTEGER DEFAULT 0")
    except: pass
    try: c.execute("ALTER TABLE render_nodes ADD COLUMN current_frame INTEGER DEFAULT 0")
    except: pass
    try: c.execute("ALTER TABLE render_nodes ADD COLUMN total_frames INTEGER DEFAULT 0")
    except: pass
    try: c.execute("ALTER TABLE render_nodes ADD COLUMN frame_time_sec INTEGER DEFAULT 0")
    except: pass
    try: c.execute("ALTER TABLE render_nodes ADD COLUMN render_type TEXT DEFAULT '图片查看器'")
    except: pass
    try: c.execute("ALTER TABLE render_nodes ADD COLUMN queue_data TEXT DEFAULT '[]'")
    except: pass
    try: c.execute("ALTER TABLE render_nodes ADD COLUMN camera_name TEXT DEFAULT ''")
    except: pass
    try: c.execute("ALTER TABLE render_nodes ADD COLUMN render_settings TEXT DEFAULT ''")
    except: pass
    # 用于记录云端是否已经推送过报警，防止疯狂重复发通知
    try: c.execute("ALTER TABLE render_nodes ADD COLUMN push_status TEXT DEFAULT ''")
    except: pass

    # 记录手机 App 的 CID 和它关注的机器
    c.execute('''
        CREATE TABLE IF NOT EXISTS app_clients (
            cid TEXT PRIMARY KEY,
            keys TEXT,
            last_active REAL
        )
    ''')
    
    conn.commit()
    conn.close()

init_db()

# ==========================================
# 📡 UniPush 云端发射引擎
# ==========================================
def send_unipush(cid, title, body):
    if not UNIPUSH_APP_ID or UNIPUSH_APP_ID == "YOUR_APP_ID":
        print("未配置 UniPush 密钥，跳过推送。")
        return
        
    try:
        # 1. 生成个推 API 鉴权 Token
        timestamp = str(int(time.time() * 1000))
        sign_str = UNIPUSH_APP_KEY + timestamp + UNIPUSH_MASTER_SECRET
        sign = hashlib.sha256(sign_str.encode('utf-8')).hexdigest()
        
        auth_url = f"https://restapi.getui.com/v2/{UNIPUSH_APP_ID}/auth"
        auth_data = {"sign": sign, "timestamp": timestamp, "appkey": UNIPUSH_APP_KEY}
        
        auth_res = requests.post(auth_url, json=auth_data, timeout=5).json()
        token = auth_res.get('data', {}).get('token')
        if not token: 
            print("UniPush 鉴权失败:", auth_res)
            return
            
        # 2. 发送单推消息
        push_url = f"https://restapi.getui.com/v2/{UNIPUSH_APP_ID}/push/single/cid"
        push_data = {
            "request_id": str(int(time.time() * 1000)),
            "audience": {"cid": [cid]},
            "push_message": {
                "notification": {
                    "title": title,
                    "body": body,
                    "click_type": "startapp" # 点击通知打开 App
                }
            }
        }
        headers = {"token": token, "Content-Type": "application/json"}
        res = requests.post(push_url, json=push_data, headers=headers, timeout=5)
        print(f"✅ 成功向 CID: {cid} 发送底层推送！响应: {res.text}")
    except Exception as e:
        print("❌ UniPush 调用异常:", e)


# ==========================================
# 📡 核心路由：C4D 上传数据并触发大脑逻辑
# ==========================================
@app.route('/api/upload', methods=['POST'])
def upload_data():
    data = request.json
    if not data or 'machine_id' not in data: return jsonify({"message": "无效的数据包"}), 400

    machine_id = data.get('machine_id')
    new_status = data.get('status', '待命')
    frame_time_sec = int(data.get('frame_time_sec', 0))
    perm_key = data.get('perm_key')
    temp_key = data.get('temp_key', '')

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    # 1. 获取这台机器之前的推送状态，防止重复报警
    c.execute("SELECT push_status FROM render_nodes WHERE machine_id=?", (machine_id,))
    row = c.fetchone()
    old_push_status = row[0] if row else ""
    
    # 2. 大脑开始判断：是否需要触发推送？
    trigger_type = None
    new_push_status = old_push_status
    
    # 条件 A：渲染完成
    if new_status == '渲染完成' and old_push_status != 'completed':
        trigger_type = 'completed'
        new_push_status = 'completed'
    # 条件 B：单帧超时 (云端预设300秒)
    elif '渲染' in new_status and frame_time_sec >= 300 and old_push_status != 'timeout':
        trigger_type = 'timeout'
        new_push_status = 'timeout'
    # 条件 C：恢复正常或开始新一帧渲染，重置报警锁
    elif frame_time_sec < 100 and new_status != '渲染完成':
        new_push_status = ''

    # 3. 如果触发了报警，找出所有正在监控这台机器的手机 CID，发射！
    if trigger_type:
        title = "✅ 渲染任务完成" if trigger_type == 'completed' else "⚠️ 渲染异常超时"
        body = f"设备 [{machine_id}] 任务已完成！" if trigger_type == 'completed' else f"设备 [{machine_id}] 单帧耗时过长，请检查。"
        
        c.execute("SELECT cid, keys FROM app_clients")
        for client in c.fetchall():
            cid = client[0]
            try:
                client_keys = json.loads(client[1])
                # 如果这个手机绑定了这台机器的密钥，就推送给它
                if perm_key in client_keys or temp_key in client_keys:
                    send_unipush(cid, title, body)
            except: pass

    # 4. 保存当前最新状态入库
    c.execute('''
        INSERT OR REPLACE INTO render_nodes 
        (machine_id, perm_key, temp_key, expire_timestamp, project, status, render_time, last_update, progress, current_frame, total_frames, frame_time_sec, render_type, queue_data, camera_name, render_settings, push_status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        machine_id, perm_key, temp_key, data.get('expire_timestamp', 0),
        data.get('project', '未知项目'), new_status, data.get('time', '--:--'), time.time(),
        data.get('progress', 0), data.get('current_frame', 0), data.get('total_frames', 0), frame_time_sec,
        data.get('render_type', '图片查看器'), json.dumps(data.get('queue_data', [])),
        data.get('camera_name', ''), data.get('render_settings', ''), new_push_status
    ))
    conn.commit()
    conn.close()
    return jsonify({"message": "云端已记录并完成校验", "code": 200})

# ==========================================
# 📡 手机 App 拉取数据 (顺便上报 CID)
# ==========================================
@app.route('/api/sync_app', methods=['POST'])
def sync_app():
    client_keys = request.json.get('keys', [])
    cid = request.json.get('cid', '')
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    # 记录该手机的 CID 和它正在关注的密钥
    if cid:
        c.execute("INSERT OR REPLACE INTO app_clients (cid, keys, last_active) VALUES (?, ?, ?)", 
                 (cid, json.dumps(client_keys), time.time()))
        conn.commit()

    authorized_nodes = []
    if not client_keys: 
        conn.close()
        return jsonify({"nodes": []})
    
    c.execute("SELECT * FROM render_nodes")
    all_nodes = c.fetchall()
    current_time = time.time()
    
    for row in all_nodes:
        m_id, perm, temp, expire, proj, status, r_time, last_upd, prog, cur_f, tot_f, f_sec, r_type, q_data, cam, r_set = row[:16]
        
        if perm in client_keys or (temp in client_keys and current_time < expire):
            authorized_nodes.append({
                "machine_id": m_id, "project": proj, "status": status, "time": r_time,
                "progress": prog, "current_frame": cur_f, "total_frames": tot_f, "frame_time_sec": f_sec,
                "render_type": r_type, "queue_data": q_data,
                "camera_name": cam, "render_settings": r_set,
                "is_online": (current_time - last_upd) < 300 
            })
    conn.close()
    return jsonify({"nodes": authorized_nodes})

@app.route('/api/verify_key', methods=['POST'])
def verify_key():
    data = request.json
    new_key = data.get('new_key')
    existing_keys = data.get('existing_keys', [])

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT machine_id, expire_timestamp FROM render_nodes WHERE perm_key=? OR temp_key=?", (new_key, new_key))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({"valid": False, "msg": "添加失败：该密钥不存在或设备从未联网。"}) 
    machine_id, expire = row
    
    if new_key.startswith('T-') and time.time() > expire:
        conn.close()
        return jsonify({"valid": False, "msg": "添加失败：该临时分享码已过期！"})
        
    if existing_keys:
        placeholders = ','.join('?' * len(existing_keys))
        query = f"SELECT machine_id FROM render_nodes WHERE perm_key IN ({placeholders}) OR temp_key IN ({placeholders})"
        c.execute(query, existing_keys + existing_keys)
        if machine_id in [r[0] for r in c.fetchall()]:
            conn.close()
            return jsonify({"valid": False, "msg": f"冲突提示：您已经拥有该设备的权限！"})
            
    conn.close()
    return jsonify({"valid": True, "msg": "密钥验证成功！", "machine_id": machine_id})

@app.route('/api/deploy', methods=['POST'])
def auto_deploy():
    try:
        repo_dir = "/home/zacharyshee/mysite"
        subprocess.run(["git", "pull", "origin", "main"], cwd=repo_dir, check=True)
        wsgi_path = "/var/www/zacharyshee_pythonanywhere_com_wsgi.py"
        subprocess.run(["touch", wsgi_path], check=True)
        return jsonify({"message": "✅ 云端代码已更新，服务器重启成功！"}), 200
    except Exception as e:
        return jsonify({"message": f"❌ 部署失败: {str(e)}"}), 500
        
if __name__ == '__main__':
    print("🚀 SaaS 中枢已升级支持原生 UniPush 推送！正在监听 5000 端口...")
    app.run(host='0.0.0.0', port=5000)