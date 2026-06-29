"""Video generation service module — PPT → narration → images + audio → MP4"""

from service.video.pipeline import run_video_pipeline
from service.video.tts import synthesize_speech

__all__ = ['run_video_pipeline', 'synthesize_speech']
