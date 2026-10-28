/**
 * dsh-graph-memory — 插件共享类型与最小 DSH 上下文声明。
 *
 * 设计约束：本插件在运行时只依赖 DSH 提供的 Cordis 上下文能力，**不 import 任何
 * @deepseek-ai/* 运行时模块**，因此既能以 npm 包安装，也能以 `link:` 本地目录安装
 * （link 安装时 Node 的 ESM 解析不会回退到 DSH 安装目录）。
 */
export {};
