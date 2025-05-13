from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import re
import urllib.parse
import os
import httpx
import logging
from typing import Dict, Any, Union

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Try to import playwright - with fallback
try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    logger.warning("Playwright not available. Will use fallback methods.")
    PLAYWRIGHT_AVAILABLE = False

app = FastAPI()

# CORS middleware for development/testing
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class DouyinResponse(BaseModel):
    aweme_id: str
    desc: str
    video_url: str
    author: str

def clean_url(url: str) -> str:
    """Extract and clean Douyin URL from input text"""
    match = re.search(r'https?://v\.douyin\.com/[a-zA-Z0-9]+', url)
    if match:
        return match.group(0)
    if url.startswith('https://www.douyin.com/'):
        return url
    return url

def extract_video_id(url: str) -> Union[str, None]:
    """Extract video ID from various Douyin URL formats"""
    video_pattern = re.search(r'/(?:video|note)/(\d+)', url)
    if video_pattern:
        return video_pattern.group(1)
    
    aweme_pattern = re.search(r'aweme_id=(\d+)', url)
    if aweme_pattern:
        return aweme_pattern.group(1)
    
    vid_pattern = re.search(r'vid=(\d+)', url)
    if vid_pattern:
        return vid_pattern.group(1)
    
    return None

async def follow_redirect(short_url: str) -> str:
    """Follow redirects to get the final URL"""
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            response = await client.get(short_url, timeout=10.0)
            return str(response.url)
    except Exception as e:
        logger.error(f"Error following redirect: {e}")
        return short_url

async def fetch_data_with_httpx(url: str, video_id: str) -> Dict[str, Any]:
    """Attempt to fetch video data using httpx"""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Referer": "https://www.douyin.com/",
            "Accept-Language": "en-US,en;q=0.9"
        }
        
        api_url = f"https://www.douyin.com/aweme/v1/web/aweme/detail/?device_platform=webapp&aid=6383&channel=channel_pc_web&aweme_id={video_id}"
        
        async with httpx.AsyncClient() as client:
            response = await client.get(api_url, headers=headers, timeout=10.0)
            if response.status_code == 200:
                data = response.json()
                if data and 'aweme_detail' in data:
                    detail = data['aweme_detail']
                    return {
                        'aweme_id': detail['aweme_id'],
                        'desc': detail['desc'],
                        'video_url': detail['video']['play_addr']['url_list'][0],
                        'author': detail['author']['nickname']
                    }
        
        # If we couldn't extract detailed data, return basic info
        return {
            "aweme_id": video_id,
            "desc": "Video Description Unavailable",
            "video_url": url,
            "author": "Unknown"
        }
        
    except Exception as e:
        logger.error(f"Error fetching data with httpx: {e}")
        return {
            "aweme_id": video_id,
            "desc": "Error retrieving video details",
            "video_url": url,
            "author": "Unknown"
        }

def get_data_with_playwright(url: str) -> Dict[str, Any]:
    """Get video data using Playwright"""
    if not PLAYWRIGHT_AVAILABLE:
        raise HTTPException(status_code=500, detail="Playwright not available")
    
    video_data = {}
    
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            
            def handle_response(response):
                nonlocal video_data
                if 'aweme/v1/web/aweme/detail' in response.url:
                    try:
                        json_data = response.json()
                        if json_data and 'aweme_detail' in json_data:
                            detail = json_data['aweme_detail']
                            video_data = {
                                'aweme_id': detail['aweme_id'],
                                'desc': detail['desc'],
                                'video_url': detail['video']['play_addr']['url_list'][0],
                                'author': detail['author']['nickname']
                            }
                    except Exception as e:
                        logger.error(f"Error processing response: {e}")
            
            page.on("response", handle_response)
            
            page.goto(url, timeout=30000)
            page.wait_for_timeout(5000)
            current_url = page.url
            
            if not video_data:
                video_id = extract_video_id(current_url)
                if video_id:
                    api_url = f"https://www.douyin.com/aweme/v1/web/aweme/detail/?device_platform=webapp&aid=6383&channel=channel_pc_web&aweme_id={video_id}"
                    page.goto(api_url)
                    page.wait_for_timeout(2000)
                
                if not video_data:
                    video_data = {
                        "aweme_id": video_id or "unknown",
                        "desc": page.title(),
                        "video_url": current_url,
                        "author": "Unknown"
                    }
            
            browser.close()
            
            return video_data
    except Exception as e:
        logger.error(f"Playwright error: {e}")
        raise HTTPException(status_code=500, detail=f"Playwright error: {str(e)}")

@app.get("/douyin", response_model=DouyinResponse)
async def get_douyin_info(url: str = Query(...)):
    """
    Get information about a Douyin video from its URL
    """
    # Clean the input URL
    url = clean_url(url)
    
    # If it's a short URL, follow redirects
    if "v.douyin.com" in url:
        url = await follow_redirect(url)
    
    # Try to extract video ID
    video_id = extract_video_id(url)
    if not video_id:
        raise HTTPException(status_code=400, detail="Could not extract video ID from URL")
    
    # Try Playwright method first if available
    if PLAYWRIGHT_AVAILABLE:
        try:
            return get_data_with_playwright(url)
        except Exception as e:
            logger.warning(f"Playwright method failed, falling back to httpx: {e}")
    
    # Fall back to httpx method
    return await fetch_data_with_httpx(url, video_id)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)