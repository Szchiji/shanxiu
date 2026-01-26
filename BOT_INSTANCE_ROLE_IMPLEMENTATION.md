# BOT_INSTANCE_ROLE Feature Implementation

## 概述 (Overview)

本实现通过环境变量 `BOT_INSTANCE_ROLE` 实现主实例与克隆实例的区分管理，确保克隆实例无法管理其他克隆实例，避免权限混乱和潜在的安全问题。

This implementation distinguishes between main and clone instances via the `BOT_INSTANCE_ROLE` environment variable, ensuring clone instances cannot manage other clones, preventing permission confusion and potential security issues.

## 实现要点 (Key Implementation Points)

### 1. 环境变量配置 (Environment Variable Configuration)

```bash
# 主实例 (Main Instance) - 默认 (default)
BOT_INSTANCE_ROLE=main

# 克隆实例 (Clone Instance)
BOT_INSTANCE_ROLE=clone
```

### 2. 核心函数 (Core Functions)

**位置 (Location):** `app/__init__.py`

```python
def get_bot_instance_role():
    """获取机器人实例角色，从环境变量读取，默认为 'main'"""
    return os.getenv('BOT_INSTANCE_ROLE', 'main').lower()

def is_clone_instance():
    """判断当前实例是否为克隆实例"""
    return get_bot_instance_role() == 'clone'

def is_main_instance():
    """判断当前实例是否为主实例"""
    return get_bot_instance_role() == 'main'
```

### 3. 上下文处理器 (Context Processor)

**位置 (Location):** `app/__init__.py`

自动向所有模板注入以下变量：
- `bot_instance_role`: 字符串，'main' 或 'clone'
- `is_clone_instance`: 布尔值
- `is_main_instance`: 布尔值

```python
@app.context_processor
def inject_bot_instance_role():
    """向所有模板注入机器人实例角色信息"""
    role = get_bot_instance_role()
    return {
        'bot_instance_role': role,
        'is_clone_instance': role == 'clone',
        'is_main_instance': role == 'main'
    }
```

### 4. 访问控制装饰器 (Access Control Decorator)

**位置 (Location):** `app/modules/core/routes.py`

```python
def require_main_instance(f):
    """装饰器：要求主实例才能访问，克隆实例访问将返回403错误"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if is_clone_instance():
            if request.is_json or request.path.startswith('/core/api/'):
                return jsonify({
                    'success': False,
                    'error': '克隆实例无权限访问此功能。Clone instances are not allowed to access this feature.'
                }), 403
            else:
                abort(403, description='克隆实例无权限访问此页面。此功能仅限主实例使用。')
        return f(*args, **kwargs)
    return decorated_function
```

### 5. 受保护的路由 (Protected Routes)

以下路由应用了 `@require_main_instance` 装饰器：

1. **页面路由:**
   - `@core_bp.route('/bot_clones')` - 克隆管理页面

2. **API路由:**
   - `@core_bp.route('/api/save_bot_clone', methods=['POST'])` - 保存/创建克隆
   - `@core_bp.route('/api/delete_bot_clone', methods=['POST'])` - 删除克隆
   - `@core_bp.route('/api/toggle_bot_clone', methods=['POST'])` - 启动/停止克隆

### 6. 前端UI调整 (Frontend UI Adjustments)

#### base.html - 侧边栏菜单隐藏

```html
{% if is_main_instance %}
<li class="nav-item">
    <a href="/core/bot_clones" class="nav-link">
        <i class="fa-solid fa-robot"></i> 机器人克隆
        <span class="feature-desc">管理机器人克隆实例</span>
    </a>
</li>
{% endif %}
```

#### bot_clones.html - 实例角色指示器

```html
<h4 class="fw-bold mb-1">
    <i class="fa-solid fa-robot me-2"></i>机器人克隆管理
    {% if is_main_instance %}
    <span class="badge bg-primary ms-2" style="font-size: 0.7rem;">主实例 Main</span>
    {% elif is_clone_instance %}
    <span class="badge bg-info ms-2" style="font-size: 0.7rem;">克隆实例 Clone</span>
    {% endif %}
</h4>
```

### 7. 启动逻辑控制 (Startup Logic Control)

**位置 (Location):** `app/modules/core/routes.py`

```python
async def start_all_clone_bots(flask_app):
    """
    启动所有激活的克隆机器人
    仅在主实例中执行，克隆实例跳过
    """
    # 克隆实例不应该启动其他克隆
    if is_clone_instance():
        print("ℹ️ 克隆实例不启动其他克隆机器人 (Clone instance does not start other clones)")
        return
    
    # ... 主实例的克隆启动逻辑
```

## 功能对比 (Feature Comparison)

| 功能 Feature | 主实例 Main | 克隆实例 Clone |
|-------------|------------|---------------|
| 访问克隆管理页面 Access clone management page | ✅ | ❌ (403) |
| 创建新克隆 Create new clone | ✅ | ❌ (403) |
| 编辑克隆配置 Edit clone config | ✅ | ❌ (403) |
| 删除克隆 Delete clone | ✅ | ❌ (403) |
| 启动/停止克隆 Start/stop clone | ✅ | ❌ (403) |
| 侧边栏显示克隆菜单 Show clone menu in sidebar | ✅ | ❌ |
| 启动时自动启动克隆 Auto-start clones on boot | ✅ | ❌ |
| 处理普通Telegram消息 Handle normal Telegram messages | ✅ | ✅ |
| 群组管理功能 Group management features | ✅ | ✅ |

## 安全特性 (Security Features)

1. **多层防护 (Multi-layer Protection)**
   - 前端UI隐藏（防止误操作）
   - 路由级别403保护（防止直接URL访问）
   - API级别403保护（防止API调用）

2. **明确错误提示 (Clear Error Messages)**
   - 页面访问: HTTP 403 + 友好错误描述
   - API调用: JSON响应 `{"success": false, "error": "..."}`

3. **启动逻辑隔离 (Startup Logic Isolation)**
   - 克隆实例启动时不会触发其他克隆的启动
   - 避免循环依赖和资源竞争

## 测试覆盖 (Test Coverage)

### 单元测试 (Unit Tests)

**文件:** `test_bot_instance_role.py`

- ✅ 角色检测函数测试
- ✅ 上下文处理器测试  
- ✅ 路由保护测试

### 综合测试 (Integration Tests)

**文件:** `test_comprehensive_instance_role.py`

- ✅ 模板渲染测试
- ✅ 启动逻辑测试
- ✅ 装饰器实现测试

### 演示脚本 (Demo Script)

**文件:** `demo_instance_role.py`

- 主实例行为演示
- 克隆实例行为演示
- 模板上下文变量说明
- 部署配置示例

## 部署示例 (Deployment Examples)

### Railway / Heroku

**主实例配置:**
```
TG_BOT_TOKEN=1234567890:AAA...
DATABASE_URL=postgresql://...
SECRET_KEY=random_secret
BOT_INSTANCE_ROLE=main  # 可选，默认就是main
```

**克隆实例配置:**
```
TG_BOT_TOKEN=9876543210:BBB...
DATABASE_URL=postgresql://...
SECRET_KEY=random_secret
BOT_INSTANCE_ROLE=clone  # 必须设置
```

### Docker Compose

```yaml
version: '3.8'

services:
  main-bot:
    build: .
    environment:
      - TG_BOT_TOKEN=${MAIN_BOT_TOKEN}
      - DATABASE_URL=${DATABASE_URL}
      - SECRET_KEY=${SECRET_KEY}
      - BOT_INSTANCE_ROLE=main
    restart: unless-stopped

  clone-bot-1:
    build: .
    environment:
      - TG_BOT_TOKEN=${CLONE_BOT_TOKEN_1}
      - DATABASE_URL=${DATABASE_URL}
      - SECRET_KEY=${SECRET_KEY}
      - BOT_INSTANCE_ROLE=clone
    restart: unless-stopped

  clone-bot-2:
    build: .
    environment:
      - TG_BOT_TOKEN=${CLONE_BOT_TOKEN_2}
      - DATABASE_URL=${DATABASE_URL}
      - SECRET_KEY=${SECRET_KEY}
      - BOT_INSTANCE_ROLE=clone
    restart: unless-stopped
```

## 文档更新 (Documentation Updates)

### README.md

- ✅ 环境变量配置示例
- ✅ BOT_INSTANCE_ROLE说明
- ✅ 主实例与克隆实例功能对比

## 向后兼容性 (Backward Compatibility)

- ✅ 未设置 `BOT_INSTANCE_ROLE` 时默认为 `main`
- ✅ 现有部署无需修改即可正常运行
- ✅ 仅在需要部署克隆实例时才需要设置环境变量

## 未来增强 (Future Enhancements)

1. **实例监控 (Instance Monitoring)**
   - 显示所有运行中的实例状态
   - 实例间通信和协调

2. **更细粒度的权限控制 (Fine-grained Permission Control)**
   - 支持更多角色类型（如 `readonly`, `manager`等）
   - 基于角色的功能权限矩阵

3. **实例负载均衡 (Instance Load Balancing)**
   - 自动在多个克隆实例间分配负载
   - 健康检查和故障转移

## 总结 (Summary)

此实现完全满足所有需求，提供了：
- ✅ 清晰的实例角色区分
- ✅ 多层次的访问控制
- ✅ 友好的用户体验
- ✅ 完整的测试覆盖
- ✅ 详细的文档说明
- ✅ 向后兼容性保证

This implementation fully satisfies all requirements, providing:
- ✅ Clear instance role distinction
- ✅ Multi-level access control
- ✅ User-friendly experience
- ✅ Complete test coverage
- ✅ Detailed documentation
- ✅ Backward compatibility guarantee
