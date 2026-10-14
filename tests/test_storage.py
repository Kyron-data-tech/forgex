import io
import pytest
from pathlib import Path
from PIL import Image
from app.services.storage import StorageService, StorageSecurityError, ImageValidationError

def create_sample_png(width=100, height=100) -> bytes:
    buf = io.BytesIO()
    img = Image.new("RGB", (width, height), color="red")
    img.save(buf, format="PNG")
    return buf.getvalue()

def test_sanitize_slug():
    assert StorageService.sanitize_slug("Marie Curie") == "marie-curie"
    assert StorageService.sanitize_slug("  Ada Lovelace (1815-1852)!  ") == "ada-lovelace-1815-1852"
    assert StorageService.sanitize_slug("René Descartes") == "rene-descartes"
    assert StorageService.sanitize_slug("") == "unnamed"

def test_sanitize_filename():
    assert StorageService.sanitize_filename("photo.jpg") == "photo.jpg"
    assert StorageService.sanitize_filename("../../../etc/passwd.png") == "passwd.png"
    assert StorageService.sanitize_filename("bad*name?file:<>.webp") == "bad_name_file.webp"

def test_safe_path_resolution(tmp_path):
    storage = StorageService(root_dir=tmp_path)
    safe_file = storage.resolve_safe_path("subfolder/file.txt")
    assert safe_file == tmp_path / "subfolder" / "file.txt"

def test_path_traversal_blocked(tmp_path):
    storage = StorageService(root_dir=tmp_path)
    with pytest.raises(StorageSecurityError):
        storage.resolve_safe_path("../outside.txt")

    with pytest.raises(StorageSecurityError):
        storage.resolve_safe_path("../../windows/system32/cmd.exe")

def test_image_validation_valid():
    storage = StorageService()
    png_data = create_sample_png(200, 200)
    fmt, w, h = storage.validate_image_bytes(png_data)
    assert fmt == "PNG"
    assert w == 200
    assert h == 200

def test_image_validation_corrupt():
    storage = StorageService()
    with pytest.raises(ImageValidationError):
        storage.validate_image_bytes(b"not an image fake binary data content here")

def test_image_size_cap():
    # Set cap lower than sample image
    storage = StorageService(max_image_size=100)
    png_data = create_sample_png(300, 300)
    with pytest.raises(ImageValidationError, match="maximum size limit"):
        storage.validate_image_bytes(png_data)

def test_user_file_deletion_blocked(tmp_path):
    storage = StorageService(root_dir=tmp_path)
    dummy_file = tmp_path / "test.txt"
    dummy_file.write_text("hello")
    with pytest.raises(PermissionError, match="strictly prohibited"):
        storage.delete_user_file(dummy_file)

def test_save_image_with_sidecar(tmp_path):
    storage = StorageService(root_dir=tmp_path)
    png_data = create_sample_png(150, 150)
    meta = {
        "source_url": "https://commons.wikimedia.org/wiki/File:Sample.png",
        "license": "CC BY-SA 4.0",
        "author": "Test Author",
        "attribution": "Photo by Test Author (CC BY-SA 4.0)"
    }
    img_path, sidecar_path = storage.save_image_with_metadata(
        person_slug="marie-curie",
        filename="portrait.png",
        image_data=png_data,
        metadata=meta
    )
    assert img_path.exists()
    assert sidecar_path.exists()
    assert "marie-curie" in str(img_path)
    assert sidecar_path.name == "portrait.png.meta.json"
