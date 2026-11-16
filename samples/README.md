# 样例数据（全部为虚构病例）

由 `python -m scripts.export_samples` 生成，供人工复核与复现。
文件：

- `centers.json`：三个中心的显示名与日期格式配置
- `mappings.json`：各中心本地编码到研究编码的映射
- `protocol-v1.0.json`：方案 v1.0 的入排与随访规则
- `upload-*.json`：各批次上传的原始行（含代表中心编码差异、单位边界、撤回与迟到病例）
