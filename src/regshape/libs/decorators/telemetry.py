#!/usr/bin/env python3

"""
:mod: `telemetry` - Module for telemetry decorators
==================================================

    module:: telemetry
    :platform: Unix, Windows
    :synopsis: Module for telemetry decorators.
    moduleauthor:: ToddySM <toddysm@gmail.com>
"""

import time

from regshape.libs.decorators.flags import TRACK_EXECUTION_TIME

def executiontime_decorator(func):
    """
    Decorator to measure the execution time of a function.

    :param func: The function to measure the execution time
    :type func: function
    """
    def wrapper(*args, **kwargs):
        start_time = time.time()
        result = func(*args, **kwargs)
        end_time = time.time()
        execution_time = round((end_time - start_time) * 1000)
        if TRACK_EXECUTION_TIME:
            print(f"Function '{func.__name__}' took {execution_time} ms to execute")
        return result
    return wrapper