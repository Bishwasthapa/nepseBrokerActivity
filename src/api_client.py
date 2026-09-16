"""
Simple Rate Limiter for NEPSE API

Provides in-memory rate limiting based on IP addresses and endpoints.
Designed to prevent abuse while being simple and lightweight.
"""

import time
import asyncio
from collections import defaultdict, deque
from typing import Dict, Optional, Tuple
from datetime import datetime, timedelta
import logging

logger = logging.getLogger(__name__)

class SimpleRateLimiter:
    """
    Simple in-memory rate limiter using sliding window approach
    """

    def __init__(self):
        # IP -> endpoint -> deque of timestamps
        self.requests: Dict[str, Dict[str, deque]] = defaultdict(lambda: defaultdict(deque))
        # IP -> last cleanup time
        self.last_cleanup: Dict[str, float] = {}

        # Rate limits (requests per minute)
        self.limits = {
            "default": 60,          # Default: 60 requests per minute
            "validation": 120,      # Validation endpoints: 120 per minute
            "market_data": 60,      # Market data: 60 per minute
            "websocket": 100,       # WebSocket connections: 100 per minute
            "websocket_message": 50, # WebSocket messages: 50 per minute
            "health": 50,          # Health checks: 50 per minute (lowered for testing)
        }

        # Time window in seconds
        self.window_size = 60  # 1 minute

        # Cleanup interval (to prevent memory leaks)
        self.cleanup_interval = 300  # 5 minutes

    def _get_endpoint_category(self, endpoint: str) -> str:
        """Categorize endpoint to determine rate limit"""
        if endpoint in ["/health"]:
            return "health"
        elif endpoint.startswith("/validate"):
            return "validation"
        elif endpoint in ["/Summary", "/LiveMarket", "/PriceVolume", "/TopGainers", "/TopLosers"]:
            return "market_data"
        elif endpoint == "websocket_connection":
            return "websocket"
        elif endpoint == "websocket_message":
            return "websocket_message"
        else:
            return "default"

    def _cleanup_old_requests(self, ip: str, endpoint: str, current_time: float):
        """Remove requests older than the window"""
        cutoff_time = current_time - self.window_size

        request_times = self.requests[ip][endpoint]
        while request_times and request_times[0] < cutoff_time:
            request_times.popleft()

    def _cleanup_old_ips(self):
        """Cleanup IPs that haven't been seen for a while"""
        current_time = time.time()
        cutoff_time = current_time - self.cleanup_interval

        ips_to_remove = []
        for ip, last_seen in self.last_cleanup.items():
            if last_seen < cutoff_time:
                ips_to_remove.append(ip)

        for ip in ips_to_remove:
            if ip in self.requests:
                del self.requests[ip]
            if ip in self.last_cleanup:
                del self.last_cleanup[ip]
            logger.debug(f"Cleaned up rate limit data for IP: {ip}")

    def is_allowed(self, ip: str, endpoint: str) -> Tuple[bool, Dict]:
        """
        Check if request is allowed
        Returns: (is_allowed, info_dict)
        """
        current_time = time.time()
        category = self._get_endpoint_category(endpoint)
        limit = self.limits[category]

        # Cleanup old requests for this IP/endpoint
        self._cleanup_old_requests(ip, endpoint, current_time)

        # Update last seen time
        self.last_cleanup[ip] = current_time

        # Periodic cleanup
        if len(self.last_cleanup) > 1000:  # Arbitrary threshold
            self._cleanup_old_ips()

        # Check current request count
        request_times = self.requests[ip][endpoint]
        current_count = len(request_times)

        # Rate limit info
        info = {
            "limit": limit,
            "remaining": max(0, limit - current_count),
            "reset_time": int(current_time + self.window_size),
            "category": category
        }

        if current_count >= limit:
            logger.warning(f"Rate limit exceeded for IP {ip} on endpoint {endpoint}: {current_count}/{limit}")
            return False, info

        # Add current request
        request_times.append(current_time)
        info["remaining"] = max(0, limit - len(request_times))

        return True, info

    def get_stats(self) -> Dict:
        """Get rate limiter statistics"""
        current_time = time.time()
        total_ips = len(self.requests)
        total_endpoints = sum(len(endpoints) for endpoints in self.requests.values())

        # Count active requests (within window)
        active_requests = 0
        for ip_requests in self.requests.values():
            for request_times in ip_requests.values():
                active_requests += sum(1 for t in request_times if current_time - t < self.window_size)

        return {
            "total_tracked_ips": total_ips,
            "total_tracked_endpoints": total_endpoints,
            "active_requests_in_window": active_requests,
            "window_size_seconds": self.window_size,
            "limits": self.limits,
            "cleanup_interval": self.cleanup_interval
        }

# Global rate limiter instance
rate_limiter = SimpleRateLimiter()

# Utility functions for different services
def check_rate_limit(ip: str, endpoint: str) -> Tuple[bool, Dict]:
    """Check rate limit for HTTP requests"""
    return rate_limiter.is_allowed(ip, endpoint)

def check_websocket_rate_limit(ip: str) -> Tuple[bool, Dict]:
    """Check rate limit for WebSocket connections"""
    return rate_limiter.is_allowed(ip, "websocket_connection")

def get_rate_limit_headers(info: Dict) -> Dict[str, str]:
    """Generate rate limit headers for HTTP responses"""
    return {
        "X-RateLimit-Limit": str(info["limit"]),
        "X-RateLimit-Remaining": str(info["remaining"]),
        "X-RateLimit-Reset": str(info["reset_time"]),
        "X-RateLimit-Category": info["category"]
    }

# Rate limit exception for FastAPI
class RateLimitExceeded(Exception):
    def __init__(self, info: Dict):
        self.info = info
        retry_after = info["reset_time"] - int(time.time())
        super().__init__(f"Rate limit exceeded. Try again in {retry_after} seconds.")
