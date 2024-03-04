import json
import math
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

import urllib3


class Config:
    webhook = ""
    logging = False
    overwrite = False
    cleanup_upscaled = False
    transcode_threads = 1

    dist_format = "avif"  # or webp, jxl but must add to formats | DO NOT USE PNG
    formats = {
        "avif": 'ffmpeg.exe -i "{input}" -c:v libsvtav1 -crf 26 -preset 6 -vf "scale=ceil(iw/2)*2:ceil(ih/2)*2" "{output}"',
        "jxl": 'cjxl.exe -d 0 -e 8 "{input}" "{output}"',
        "djxl": 'djxl.exe "{input}" "{output}"',
        "png": 'ffmpeg.exe -i "{input}" -c:v png -compression_level 6 "{output}"',
        "mp4": 'ffmpeg.exe -i "{input}" -c:v libx264 -c:a copy -crf 22 -preset slow -vf "1920:-2" "{output}"',
    }

    use_tpai = False

    # These options will be ignored if use_tpai is True
    dist_width = 2500
    always_upscale = False
    resize_threads = 4
    files_to_upscale = [".png", ".jpg", ".jpeg", ".webp"]


class BCOLORS:
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    BLUE = "\033[94m"
    RESET = "\033[0m"


def norm(path: str) -> str:
    """os.path.normpath() but replace backslash with forward slash"""
    return os.path.normpath(path).replace("\\", "/")


def notify(title: str, description: str):
    """Send a notification to Discord"""
    http = urllib3.PoolManager()
    http.request(
        "POST",
        Config.webhook,
        headers={"Content-Type": "application/json"},
        body=json.dumps(
            {
                "embeds": [{"title": title, "description": description}],
                "username": "Gallery preprocessor",
            }
        ),
    )


def list_files(path: str, formats: list[str], recursive: bool = False) -> list[str]:
    """List all files in a directory with specific formats"""
    if path == "" or not os.path.exists(path) or len(formats) == 0:
        return []

    files: list[str] = []
    for i in os.listdir(path):
        if os.path.isfile(os.path.join(path, i)) and i.endswith(tuple(formats)):
            files.append(norm(os.path.join(path, i)))
        elif os.path.isdir(os.path.join(path, i)) and recursive:
            files += list_files(os.path.join(path, i), formats, recursive)
    return files


def warn(msg: str):
    print(f"  ⚠️  {msg}")


class Transcoder:
    def single(self, in_file: str, out_file: str, out_format: str):
        """Run the transcoding command and return the status"""
        if os.path.exists(out_file) and not Config.overwrite:
            warn(f"{os.path.basename(out_file)} already exists, skipping...")
            return
        if os.path.exists(out_file):
            os.remove(out_file)

        cmd = Config.formats[out_format]

        if os.path.splitext(in_file)[1] == ".jxl" and out_format == "png":
            cmd = Config.formats["djxl"]

        cmd = cmd.format(input=in_file, output=out_file)
        stdout = open(f"transcode_{out_format}.log", "a") if Config.logging else subprocess.DEVNULL

        if (shutil.which("ffpb") is not None) and (out_format == "mp4"):
            stdout = None
            cmd = cmd.replace("ffmpeg", "ffpb")

        subprocess.run(cmd, shell=True, stdout=stdout, stderr=stdout)

        if os.path.exists(out_file) and os.path.getsize(out_file) > 0:
            return
        print(f"  ❌ {os.path.basename(in_file)} failed to transcode")

    def batch(self, in_files: list[str], out_dir: str, out_format: str):
        in_files_empty = len(in_files) == 0
        out_dir_is_a_file = os.path.isfile(out_dir)
        out_format_not_valid = out_format not in Config.formats
        if in_files_empty or out_dir_is_a_file or out_format_not_valid:
            warn("in files is empty, out_dir is a file or out_format is not valid")
            return

        os.mkdir(out_dir) if not os.path.exists(out_dir) else None
        threads = 1 if out_format in ("avif", "mp4") else Config.transcode_threads

        out_files: list[str] = [
            os.path.splitext(os.path.join(out_dir, os.path.basename(in_file)))[0] + "." + out_format for in_file in in_files
        ]

        with ThreadPoolExecutor(max_workers=threads) as executor:
            futures = [executor.submit(self.single, i, o, out_format) for i, o in zip(in_files, out_files)]
            for future in as_completed(futures):
                future.result()


class Resizer:
    def single(self, in_file: str, out_file: str):
        if os.path.exists(out_file) and not Config.overwrite:
            os.remove(out_file)
        basename = os.path.basename(in_file)

        # Img dimension
        cmd = subprocess.run(
            f'ffprobe.exe -v error -select_streams v:0 -show_entries stream=width,height -of csv=s=x:p=0 "{in_file}"',
            shell=True,
            capture_output=True,
        )
        if cmd.returncode != 0:
            print(f"  ❌ {basename} failed to get dimensions")
            return

        width, height = cmd.stdout.decode("utf-8").split("x")
        width, height = int(width), int(height)

        # Upscale ratio
        scale = min(math.ceil(Config.dist_width / width), 4)
        if scale == 1 and not Config.always_upscale:
            shutil.copy(in_file, out_file)
            warn(f"{basename} is already larger than the target size")
            return
        scale = max(scale, 2)

        out_file = os.path.splitext(out_file)[0] + ".png"

        # Upscale
        temp_file = f"{out_file}.temp.png"
        subprocess.run(
            f'realesrgan-ncnn-vulkan.exe -i "{in_file}" -o "{temp_file}" -s {scale} -n realesr-animevideov3 -f png',
            shell=True,
            capture_output=True,
        )
        if not os.path.exists(temp_file):
            print(f"  ❌ {basename} failed to upscale")
            return

        # Resize if needed
        if width * scale > Config.dist_width:
            log_file = open("upscale.log", "a") if Config.logging else subprocess.DEVNULL
            subprocess.run(
                f'ffmpeg -i "{temp_file}" -vf "scale={Config.dist_width}:-1" -y "{out_file}"',
                shell=True,
                stdout=log_file,
                stderr=log_file,
            )
            if not os.path.exists(out_file):
                print(f"  ❌ {basename} failed to downscale after upscaling")
                return
            os.remove(temp_file)
            return

        os.rename(temp_file, out_file)

    def batch_realesrgan(self, in_files: list[str], out_dir: str):
        if len(in_files) == 0 or os.path.isfile(out_dir):
            warn("in files is empty or out_dir is a file")
            return
        os.mkdir(out_dir) if not os.path.exists(out_dir) else None

        out_files = [os.path.join(out_dir, os.path.basename(i)) for i in in_files]

        with ThreadPoolExecutor(max_workers=Config.resize_threads) as executor:
            futures = [executor.submit(self.single, i, o) for i, o in zip(in_files, out_files)]
            for future in as_completed(futures):
                future.result()

    def batch_tpai(self, in_dir: str, out_dir: str):
        """Invoke tpai.exe <in_dir> -o <out_dir> -f png"""
        if os.path.exists(out_dir):
            warn("already upscaled")
            return

        stdout = open("resize.log", "a") if Config.logging else subprocess.DEVNULL
        cmd = f'tpai.exe "{in_dir}" -o "{out_dir}" -f png'
        subprocess.run(cmd, shell=True, stdout=stdout, stderr=stdout)

    def batch(self, in_dir: str, out_dir: str):
        if Config.use_tpai:
            self.batch_tpai(in_dir, out_dir)
        else:
            self.batch_realesrgan(list_files(in_dir, Config.files_to_upscale), out_dir)


def single_compress(input_dir: str, output_file: str, format: str):
    """Archive dir w/ 7z, format: 7z or zip"""
    if not os.path.exists(input_dir) or not os.path.isdir(input_dir):
        warn(f"{input_dir} does not exist or is not a directory")

    out_file = os.path.splitext(output_file)[0] + "." + format
    os.remove(out_file) if os.path.exists(out_file) else None

    os.chdir(input_dir)
    cmd = subprocess.run(f'7z.exe a -bt -t{format} -mx1 -r "{out_file}"', shell=True, capture_output=True)
    if cmd.returncode != 0:
        print(f"  ❌ Failed to archive {input_dir} to {format}")
    else:
        shutil.move(out_file, "..")
    os.chdir("..")


class MainMenu:
    def __init__(self, options: dict[int | str, str]):
        for key, value in options.items():
            if str(key).startswith("div"):
                print("─" * 30)
            else:
                print(f"{BCOLORS.YELLOW}[{key}]{BCOLORS.RESET} {value}")
        print("─" * 30)

    # ----- HELPER FUNCTIONS -----

    def __print_small_sign(self, msg: str):
        R, B, RE = BCOLORS.RED, BCOLORS.BLUE, BCOLORS.RESET
        print(f"{R}|=====[{RE}{B}{msg}{RE}{R}|====|{RE}")

    def __endswith(self, string: str, suffixes: list[str]) -> bool:
        for i in suffixes:
            if string.endswith(i):
                return True
        return False

    def __get_process_dirs(self) -> list[str]:
        """
        Get a list of dirs that are processable
        - Does not exist <dir>.7z or <dir>.zip
        - Contains images (png, jpg, jpeg, webp)
        - Does not exist <dir>_dist || <dir>_archive || <dir>_upscaled
        """
        return_list: list[str] = []
        for i in os.listdir():
            if not os.path.isdir(i):
                continue
            if any([self.__endswith(i, [".7z", ".zip"]) for i in os.listdir(i)]):
                continue
            if not any([self.__endswith(i, [".png", ".jpg"]) for i in os.listdir(i)]):
                continue
            if any([self.__endswith(i, ["_dist", "_archive", "_upscaled"]) for i in os.listdir(i)]):
                continue
            return_list.append(i)
        return return_list

    def __get_reprocess_dirs(self) -> list[str]:
        """
        Get a list of dirs containing .jxl, .mp4, .webm, .gif
        """
        return_list: list[str] = []
        for i in os.listdir():
            if not os.path.isdir(i):
                continue
            if not any([self.__endswith(i, [".jxl", ".mp4", ".webm", ".gif"]) for i in os.listdir(i)]):
                continue
            return_list.append(i)
        return return_list

    # ----- MAIN FUNCTIONS -----

    def __process_one(self, input_dir: str):
        transcoder, resizer = Transcoder(), Resizer()

        print()
        self.__print_small_sign(os.path.basename(input_dir))

        upscale_dir = input_dir + "_upscaled"
        print("👉 input -> upscaled: any -> PNG (upscaled)")
        resizer.batch(input_dir, upscale_dir)

        dist_dir = input_dir + "_dist"
        print(f"👉 input -> dist: PNG (upscaled) -> {Config.dist_format} (dist)")
        transcoder.batch(list_files(upscale_dir, [".png"]), dist_dir, Config.dist_format)

        print("👉 input -> dist: animations -> x264 (dist)")
        transcoder.batch(list_files(input_dir, [".mp4", ".webm"]), dist_dir, "mp4")

        if Config.cleanup_upscaled:
            print(f"👉 Cleaning up {upscale_dir}")
            shutil.rmtree(upscale_dir)

        archive_dir = input_dir + "_archive"
        print("👉 input -> archive: PNG, JPG, GIF -> JXL (compressed)")
        os.mkdir(archive_dir) if not os.path.exists(archive_dir) else None
        img_to_transcode = list_files(input_dir, [".png", ".jpg", "jpeg", ".gif"], True)
        transcoder.batch(img_to_transcode, input_dir + "_archive", "jxl")

        print("👉 input -> archive: MP4, WEBP, WEBP -> copy")
        for i in list_files(input_dir, [".mp4", ".webp", ".webm"], True):
            shutil.copy(os.path.basename(i), os.path.join(archive_dir, os.path.basename(i)))

        print("👉 archive -> .7z...")
        single_compress(archive_dir, input_dir, "7z")

        print("👉 dist -> .zip...")
        single_compress(dist_dir, input_dir, "zip")

    def __reprocess_one(self, input_dir: str):
        transcoder, resizer = Transcoder(), Resizer()

        print()
        self.__print_small_sign(os.path.basename(input_dir))

        png_dir = input_dir + ".png"
        print("👉 input -> png dir: JXL -> PNG")
        transcoder.batch(list_files(input_dir, [".jxl"]), png_dir, "png")

        upscaled_dir = input_dir + "_upscaled"
        print("👉 png dir -> upscaled: PNG -> PNG (upscaled)")
        resizer.batch(png_dir, upscaled_dir)

        dist_dir = input_dir + "_dist"
        print(f"👉 upscaled -> dist: PNG (upscaled) -> {Config.dist_format} (dist)")
        transcoder.batch(list_files(upscaled_dir, [".png"]), dist_dir, Config.dist_format)

        print("👉 input -> dist: animations -> x264 (dist)")
        transcoder.batch(list_files(input_dir, [".mp4", ".webm"]), dist_dir, "mp4")

        print("👉 dist -> .zip...")
        single_compress(dist_dir, input_dir, "zip")

        if Config.cleanup_upscaled:
            print("👉 cleanup upscaled dir")
            shutil.rmtree(upscaled_dir)

    def one_pack(self, reprocess: bool = False):
        dirs = self.__get_reprocess_dirs() if reprocess else self.__get_process_dirs()
        if len(dirs) == 0:
            return

        print(f"Select a dir to {'reprocess' if reprocess else 'process'}:")
        for i in range(len(dirs)):
            print(f"  [{i}] {dirs[i]}")
        selected_idx = input("⌨️  ")
        if not selected_idx.isdigit() or int(selected_idx) not in range(len(dirs)):
            return

        if reprocess:
            self.__reprocess_one(dirs[int(selected_idx)])
            return

        self.__process_one(dirs[int(selected_idx)])


def main():
    last_msg = ""
    while True:
        os.system("cls" if os.name == "nt" else "clear")
        print(last_msg) if last_msg != "" else None
        last_msg = ""

        menu = MainMenu(
            {
                1: "Process multiple packs",
                2: "Process one pack",
                "div1": "",
                3: "Reprocess multiple packs",
                4: "Reprocess one pack",
                "div2": "",
                "else": "Exit",
            }
        )

        user_input = input("Select an option: ")
        match user_input:
            case "1":
                # menu.multiple_pack()
                pass
            case "2":
                menu.one_pack()
            case "3":
                pass
                # menu.multiple_pack(reprocess=True)
            case "4":
                pass
                menu.one_pack(reprocess=True)
            case _:
                raise KeyboardInterrupt

        input("Press enter to continue...")


if __name__ == "__main__":
    for binary in ("ffmpeg.exe", "ffprobe.exe", "7z.exe", "cjxl.exe"):
        if shutil.which(binary) is None:
            print(f"Binary {binary} not found in PATH")

    if Config.use_tpai and not shutil.which("tpai.exe"):
        print("Binary tpai.exe not found in PATH")
    elif not shutil.which("realsergan-ncnn-vulkan.exe"):
        print("Binary realsergan-ncnn-vulkan.exe not found in PATH")

    try:
        main()
    except KeyboardInterrupt:
        print("\nExiting...")
        exit(0)
