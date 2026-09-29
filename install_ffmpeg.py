"""
Script to automatically download and install ffmpeg for Windows.
This script downloads a portable version of ffmpeg and extracts it locally.
"""
import os
import sys
import zipfile
import urllib.request
import shutil
from pathlib import Path

FFMPEG_DIR = os.path.join(os.path.dirname(__file__), "ffmpeg")
FFMPEG_BIN = os.path.join(FFMPEG_DIR, "bin", "ffmpeg.exe")
FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"

def download_file(url, destination):
    """Download a file from URL to destination."""
    print(f"📥 Downloading ffmpeg from: {url}")
    print(f"   Saving to: {destination}")
    
    def progress_hook(count, block_size, total_size):
        percent = int(count * block_size * 100 / total_size)
        if percent % 10 == 0:
            print(f"   Progress: {percent}%", end='\r')
    
    try:
        urllib.request.urlretrieve(url, destination, reporthook=progress_hook)
        print(f"\n✅ Download complete!")
        return True
    except Exception as e:
        print(f"\n❌ Download failed: {e}")
        return False

def extract_ffmpeg(zip_path, extract_to):
    """Extract ffmpeg from zip file."""
    print(f"📦 Extracting ffmpeg...")
    
    try:
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            # Find the ffmpeg.exe in the zip
            members = zip_ref.namelist()
            
            # Extract all files
            zip_ref.extractall(extract_to)
            
            # Find the actual ffmpeg directory (usually named like "ffmpeg-6.x-essentials_build")
            extracted_dirs = [d for d in os.listdir(extract_to) if os.path.isdir(os.path.join(extract_to, d)) and 'ffmpeg' in d.lower()]
            
            if extracted_dirs:
                # Move contents from extracted directory to our target directory
                source_dir = os.path.join(extract_to, extracted_dirs[0])
                for item in os.listdir(source_dir):
                    src = os.path.join(source_dir, item)
                    dst = os.path.join(extract_to, item)
                    if os.path.exists(dst):
                        if os.path.isdir(dst):
                            shutil.rmtree(dst)
                        else:
                            os.remove(dst)
                    shutil.move(src, dst)
                # Remove the now-empty extracted directory
                os.rmdir(source_dir)
            
            print(f"✅ Extraction complete!")
            return True
    except Exception as e:
        print(f"❌ Extraction failed: {e}")
        import traceback
        traceback.print_exc()
        return False

def install_ffmpeg():
    """Main installation function."""
    print("="*80)
    print("🎬 FFMPEG INSTALLATION")
    print("="*80)
    print()
    
    # Check if already installed
    if os.path.exists(FFMPEG_BIN):
        print(f"✅ ffmpeg is already installed at: {FFMPEG_BIN}")
        return True
    
    # Create ffmpeg directory
    os.makedirs(FFMPEG_DIR, exist_ok=True)
    
    # Download zip file
    zip_path = os.path.join(FFMPEG_DIR, "ffmpeg.zip")
    
    if not download_file(FFMPEG_URL, zip_path):
        print("\n❌ Failed to download ffmpeg")
        print("   Please download manually from: https://www.gyan.dev/ffmpeg/builds/")
        print("   Extract to:", FFMPEG_DIR)
        return False
    
    # Extract
    if not extract_ffmpeg(zip_path, FFMPEG_DIR):
        print("\n❌ Failed to extract ffmpeg")
        return False
    
    # Clean up zip file
    try:
        os.remove(zip_path)
    except:
        pass
    
    # Verify installation
    if os.path.exists(FFMPEG_BIN):
        print(f"\n✅ ffmpeg installed successfully!")
        print(f"   Location: {FFMPEG_BIN}")
        return True
    else:
        print(f"\n❌ ffmpeg.exe not found after installation")
        print(f"   Expected location: {FFMPEG_BIN}")
        return False

if __name__ == "__main__":
    try:
        success = install_ffmpeg()
        if success:
            print("\n" + "="*80)
            print("✅ Installation complete! You can now use video rendering.")
            print("="*80)
            sys.exit(0)
        else:
            print("\n" + "="*80)
            print("❌ Installation failed. Please install ffmpeg manually.")
            print("="*80)
            sys.exit(1)
    except KeyboardInterrupt:
        print("\n\n⚠️  Installation cancelled by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n\n❌ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

