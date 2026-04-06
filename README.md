# ChatGPT Register Workbench

单页工作台版 ChatGPT 注册机。

## 目录

- `backend/` FastAPI + SQLite 后端
- `frontend/` React + Vite 单页 WebUI

## 启动后端

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cd backend
python main.py
```

## 启动前端

```bash
cd frontend
npm install
npm run dev
```

前端 WebUI 开发端口为 `7788`，访问：

```text
http://127.0.0.1:7788
```

前端会自动代理 `/api` 到后端 `http://127.0.0.1:8000`。

当前 WebUI 已固定为：

- 只走 **有 RT** 注册链路
- 必须成功获取 **Refresh Token**
- 不再展示“验证码方案”配置
- 不再展示“注册模式”配置
