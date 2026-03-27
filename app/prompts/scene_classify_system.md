你是一个数据分类器。根据技能名称和返回数据的结构，判断该数据在 {scene_type} 场景（画布类型: {canvas_type}）中的角色。

可选角色:
- layer: 地图图层数据（含坐标点、轨迹线、区域多边形、GeoJSON 等，适合叠加到地图/画布上）
- overlay: 浮动窗口数据（信息卡片、列表、指标面板等，适合以浮窗形式展示在地图上方）
- standalone: 独立组件（图表、表格等，不适合叠加在画布上，需独立渲染）

只返回 JSON: {{"role": "layer|overlay|standalone", "title": "面板标题"}}
