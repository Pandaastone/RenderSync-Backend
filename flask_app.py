import sqlite3
import time
import json
import subprocess
import os
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

DB_FILE = 'rendersync.db'

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
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
    
    conn.commit()
    conn.close()

init_db()

@app.route('/api/upload', methods=['POST'])
def upload_data():
    data = request.json
    if not data or 'machine_id' not in data: return jsonify({"message": "无效的数据包"}), 400

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        INSERT OR REPLACE INTO render_nodes 
        (machine_id, perm_key, temp_key, expire_timestamp, project, status, render_time, last_update, progress, current_frame, total_frames, frame_time_sec, render_type, queue_data, camera_name, render_settings)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        data.get('machine_id'), data.get('perm_key'), data.get('temp_key', ''), data.get('expire_timestamp', 0),
        data.get('project', '未知项目'), data.get('status', '待命'), data.get('time', '--:--'), time.time(),
        data.get('progress', 0), data.get('current_frame', 0), data.get('total_frames', 0), data.get('frame_time_sec', 0),
        data.get('render_type', '图片查看器'), json.dumps(data.get('queue_data', [])),
        data.get('camera_name', ''), data.get('render_settings', '')
    ))
    conn.commit()
    conn.close()
    return jsonify({"message": "云端已记录", "code": 200})

@app.route('/api/sync_app', methods=['POST'])
def sync_app():
    client_keys = request.json.get('keys', [])
    if not client_keys: return jsonify({"nodes": []})

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    authorized_nodes = []
    
    c.execute("SELECT * FROM render_nodes")
    all_nodes = c.fetchall()
    current_time = time.time()
    
    for row in all_nodes:
        m_id, perm, temp, expire, proj, status, r_time, last_upd, prog, cur_f, tot_f, f_sec, r_type, q_data, cam, r_set = row
        
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
    # 【核心修改】：现在后端会把机器原本的 ID 发给前端，供前端当做默认备注名
    return jsonify({"valid": True, "msg": "密钥验证成功！", "machine_id": machine_id})


# ==========================================
# 自动化部署 Webhook 接口
# ==========================================
@app.route('/api/deploy', methods=['POST'])
def auto_deploy():
    try:
        # 1. 明确指定你的项目工作目录（修复拉取迷路问题）
        repo_dir = "/home/zacharyshee/mysite"
        
        # 强制在这个目录下执行 git pull
        subprocess.run(["git", "pull", "origin", "main"], cwd=repo_dir, check=True)
        
        # 2. 你的真实用户名 zacharyshee 的重启开关路径
        wsgi_path = "/var/www/zacharyshee_pythonanywhere_com_wsgi.py"
        subprocess.run(["touch", wsgi_path], check=True)
        
        return jsonify({"message": "✅ 云端代码已更新，服务器重启成功！"}), 200
    except Exception as e:
        return jsonify({"message": f"❌ 部署失败: {str(e)}"}), 500
        
        
if __name__ == '__main__':
    print("🚀 SaaS 中枢已升级支持设备别名系统！正在监听 5000 端口...")
    app.run(host='0.0.0.0', port=5000)