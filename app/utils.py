# -*- coding: utf-8 -*-
from __future__ import annotations
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo
from loguru import logger

def timezone_filter(record):
    record["time"] = record["time"].astimezone(ZoneInfo("Asia/Shanghai"))
    return record

def init_log(**sink_channel):
    # 简单的日志初始化，不再包含任何补丁逻辑
    log_level = os.getenv("LOG_LEVEL", "DEBUG").upper()
    for path in sink_channel.values():
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.add(sink=sys.stdout, level=log_level, diagnose=False, backtrace=False, filter=timezone_filter)
    
    # 挂载其他日志输出
    if sink_channel.get("error"):
        logger.add(sink=sink_channel.get("error"), level="ERROR", rotation="5 MB", diagnose=False, backtrace=False, filter=timezone_filter)
    if sink_channel.get("runtime"):
        logger.add(sink=sink_channel.get("runtime"), level="TRACE", rotation="5 MB", diagnose=False, backtrace=False, filter=timezone_filter)
        
    return logger
