import math
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests


class Config:
    webhook = ""
    logging = True
    overwrite = False
    use_higher_quality_model_for_4x = False
    force_higher_quality_model = False # if True overrides use_higher_quality_model_for_4x

class BCOLORS:
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    BLUE = "\033[94m"
    RESET = "\033[0m"


def norm(path: str) -> str:
    """os.path.normpath() but replace backslash with forward slash"""
    return os.path.normpath(path).replace("\\", "/")


def notify(title: str, description: str, username: str = "Gallery preprocessor"):
    """
    Send a notification to Discord

    Params
    - title (str): The title of the notification
    - description (str): The description of the notification
    - username (str): The username of the webhook
    """
    requests.post(
        Config.webhook,
        json={
            "embeds": [{"title": title, "description": description}],
            "username": username,
        },
    )


def list_files(path: str, ext: list[str], recursive: bool = False) -> list[str]:
    """
    List all files in a directory with a specific extension

    Params
    - path (str): The path to the directory
    - ext (tuple[str]): The extension of the files to list
    - recursive (bool): Whether to list files recursively
    """
    if path == "":
        return []

    files: list[str] = []
    for i in os.listdir(path):
        if os.path.isfile(os.path.join(path, i)) and i.endswith(tuple(ext)):
            files.append(norm(os.path.join(path, i)))
        elif os.path.isdir(os.path.join(path, i)) and recursive:
            files += list_files(os.path.join(path, i), ext, recursive)
    return files


def get_dimension(file: str) -> tuple[int, int]:
    """
    Get the dimensions of an image

    Params
    - file (str): The path to the image

    Returns
    - (int, int): The width and height of the image, (-1, -1) if failed to get dimensions (e.g. file not found)
    """
    ffprobe_output = subprocess.run(
        f'ffprobe -v error -select_streams v:0 -show_entries stream=width,height -of csv=s=x:p=0 "{file}"',
        shell=True,
        capture_output=True,
    )
    if ffprobe_output.returncode != 0:
        return -1, -1

    width, height = ffprobe_output.stdout.decode("utf-8").split("x")
    width, height = int(width), int(height)
    return width, height


def batch_transcode(
    in_files: list[str], out_folder: str = "", format: str = "avif", overwrite: bool = False, threads: int = 4
) -> tuple[list[str], list[str]]:
    """
    Transcode a list of files to a specific format

    Params
    - in_files (list[str]): A tuple of files to transcode
    - out_folder (str): The path to the output folder
    - threads (int): Number of threads to use
    - format (str): The format to transcode to (avif, jxl, webp, png

    Returns
    - (list[str]): files that failed to transcode
    - (list[str]): files that skipped transcoding

    Notes
    - If out_folder == "": <parent>_<format>/.../<original_file_name>.<format>
    - format == mp4 will show progress (fallback to ffmpeg if ffpb not in PATH, install with `pip install ffpb`)
    - .avif and .mp4 files will be transcoded with 1 thread, regardless of the threads parameter
    """
    if len(in_files) == 0:
        return [], []

    commands = {
        "avif": 'ffmpeg -i "{input}" -c:v libsvtav1 -pix_fmt yuv420p10le -crf 24 -preset 6 -vf "scale=ceil(iw/2)*2:ceil(ih/2)*2"{overwrite_flag} "{output}"',
        "jxl": 'cjxl -q 100 -e 8 "{input}" "{output}"',
        "png": 'ffmpeg -i "{input}" -c:v png -compression_level 6{overwrite_flag} "{output}"',
        "mp4": 'ffmpeg -i "{input}" -c:v libsvtav1 -pix_fmt yuv420p10le -crf 24 -preset 6 -vf "scale=-1:\'min(1440,ih)\'"{overwrite_flag} "{output}"',
    }

    if format in ("avif", "mp4"):
        threads = 1

    out_files: list[str] = []
    for file in in_files:
        path_elements = norm(file).split("/")
        if len(path_elements) > 1:  # sits in a folder
            path_elements[0] = out_folder if (out_folder != "") else (path_elements[0] + f"_{format}")
        path_elements[-1] = os.path.splitext(path_elements[-1])[0] + f".{format}"  # change extension
        out_file = os.path.join(*path_elements)
        if (out_dir := os.path.dirname(out_file)) and (not os.path.exists(out_dir)):  # create out_dir if not exists
            os.makedirs(out_dir)
        out_files.append(out_file)

    overwrite_flag = " -y" if overwrite else ""

    def __helper(in_file: str, out_file: str) -> tuple[str, str]:
        """Run the transcoding command and return the status
        Returns tuple[<success|failed|skipped>, <in_file>]
        """
        nonlocal overwrite_flag, format, threads
        if os.path.exists(out_file) and not overwrite:
            return "skipped", in_file

        cmd = commands[format]
        cmd = cmd.format(input=in_file, overwrite_flag=overwrite_flag, output=out_file)

        sp_output = open(f"transcode_{format}.log", "a") if Config.logging else subprocess.DEVNULL
        if (shutil.which("ffpb") is not None) and (format == "mp4"):
            sp_output = None
            cmd = cmd.replace("ffmpeg", "ffpb")

        subprocess.run(
            cmd,
            shell=True,
            stdout=sp_output,
            stderr=sp_output,
        )
        if os.path.exists(out_file) and os.path.getsize(out_file) > 0:
            return "success", in_file
        return "failed", in_file

    return_data: dict[str, list[str]] = {
        "failed": [],
        "skipped": [],
    }

    def print_status(status: str, path: str):
        color = BCOLORS.RED if status == "failed" else BCOLORS.YELLOW if status == "skipped" else BCOLORS.GREEN
        print(f"  {color}{status}{BCOLORS.RESET} {os.path.basename(path)}")

    if threads > 1:
        with ThreadPoolExecutor(max_workers=threads) as executor:
            futures = [executor.submit(__helper, i, o) for i, o in zip(in_files, out_files)]
            for future in as_completed(futures):
                status, file = future.result()
                print_status(status, file)
                return_data[status].append(file) if status != "success" else None
    else:
        for i, o in zip(in_files, out_files):
            status, file = __helper(i, o)
            print_status(status, file)
            return_data[status].append(file)

    return return_data["failed"], return_data["skipped"]


def single_upscale(
    in_file: str, out_file: str, width: int, height: int, target_width: int, target_height: int
) -> tuple[bool, str]:
    """
    Upscale a single image

    Params
    - in_file (str): The path to the input image
    - out_file (str): The path to the output image
    - width (int): The width of the input image
    - height (int): The height of the input image
    - target_width (int): The target width
    - target_height (int): The target height

    Returns
    - (bool): Whether upscaling succeeded
    - (str): The error message if upscaling failed, otherwise the input_path
    """

    if target_width == 0 and target_height == 0:
        return False, "Both target width and height cannot be 0"
    ffprobe_output = subprocess.run(
        f'ffprobe -v error -select_streams v:0 -show_entries stream=width,height -of csv=s=x:p=0 "{in_file}"',
        shell=True,
        capture_output=True,
    )
    if ffprobe_output.returncode != 0:
        return False, ffprobe_output.stderr.decode("utf-8")

    width_only = target_width != 0 and target_height == 0
    height_only = target_width == 0 and target_height != 0

    # ----- Calculate scale and upscale ----- #
    scale = max(math.ceil(target_width / width), math.ceil(target_height / height))
    if width_only:
        scale = math.ceil(target_width / width)
    elif height_only:
        scale = math.ceil(target_height / height)
    scale = min(scale, 4)

    model = "realesrgan-x4plus-anime" if scale == 4 else "realesr-animevideov3"
    subprocess.run(
        f'realesrgan-ncnn-vulkan -i "{in_file}" -o "{out_file}" -s {scale} -n {model} -f png',
        shell=True,
        capture_output=True,
    )

    # ----- Resize to target size if needed ----- #
    do_resize = False
    filter: str = ""
    if width_only and width * scale > target_width:
        filter = f"scale={target_width}:-1"
        do_resize = True
    elif height_only and height * scale > target_height:
        filter = f"scale=-1:{target_height}"
        do_resize = True
    elif not width_only and not height_only and (width * scale > target_width or height * scale > target_height):
        filter = f"scale={target_width}:{target_height}"
        do_resize = True
    if do_resize:
        log_file = open("upscale.log", "a") if Config.logging else subprocess.DEVNULL
        subprocess.run(
            f'ffmpeg -i "{out_file}" -vf "{filter}" -y "{out_file}.png"',  # <output_path>.png.png
            shell=True,
            stdout=log_file,
            stderr=log_file,
        )
        os.remove(out_file)
        os.rename(f"{out_file}.png", out_file)

    if os.path.exists(out_file):
        return True, in_file
    return False, "Downscaling after upscaling failed"


def batch_resize(
    in_files: list[str], out_folder: str, threads: int = 4, target_width: int = 0, target_height: int = 0
) -> list[str]:
    """
    Upscale a list of images

    Params
    - in_files (list[str]): A list of images to upscale
    - out_folder (str): The folder to output the upscaled images
    - threads (int): Number of threads to use
    - target_width (int): The target width
    - target_height (int): The target height

    Returns
    - (list[str]): A list of images that failed to upscale

    Notes
    - Out_folder == "": <parent>_upscaled/.../<original_file_name>.png
    """
    if in_files == []:
        return []

    out_paths: list[str] = []
    for file in in_files:
        path_elements = norm(file).split("/")
        if len(path_elements) > 1:  # sits in a folder
            path_elements[0] = out_folder
        path_elements[-1] = os.path.splitext(path_elements[-1])[0] + ".png"  # change extension
        output_path = "/".join(path_elements)
        if (out_dir := os.path.dirname(output_path)) and (not os.path.exists(out_dir)):  # create out_dir if not exists
            os.makedirs(out_dir)
        out_paths.append(output_path)

    def helper(in_path: str, out_path: str) -> tuple[bool, str]:
        """Run the upscaling command and return the status
        Returns
        - (bool): Whether upscaling succeeded
        - (str): The path of the input file
        """
        nonlocal target_width, target_height
        if os.path.exists(out_path) and not Config.overwrite:
            return True, ""
        width, height = get_dimension(in_path)

        if width == -1 or height == -1:
            return False, in_path

        if target_width != 0 and width >= target_width:
            # use ffmpeg to downscale
            log_file = open("downscale.log", "a") if Config.logging else subprocess.DEVNULL
            subprocess.run(
                f'ffmpeg -i "{in_path}" -vf "scale={target_width}:-1" -y "{out_path}"',
                shell=True,
                stdout=log_file,
                stderr=log_file,
            )
            if os.path.exists(out_path):
                return True, ""
            return False, in_path

        if target_height != 0 and height >= target_height:
            # use ffmpeg to downscale
            log_file = open("downscale.log", "a") if Config.logging else subprocess.DEVNULL
            subprocess.run(
                f'ffmpeg -i "{in_path}" -vf "scale=-1:{target_height}" -y "{out_path}"',
                shell=True,
                stdout=log_file,
                stderr=log_file,
            )
            if os.path.exists(out_path):
                return True, ""
            return False, in_path

        return single_upscale(in_path, out_path, width, height, target_width, target_height)

    failed: list[str] = []
    with ThreadPoolExecutor(max_workers=threads) as executor:
        futures = [executor.submit(helper, i, o) for i, o in zip(in_files, out_paths)]
        for future in as_completed(futures):
            is_success, file = future.result()
            if not is_success:
                failed.append(file)

    return failed


def single_compress(input_path: str, output_path: str, format: str, whitelist_ext: list[str] = ["*"]) -> str:
    """
    Compress a directory with 7z

    Params
    - input_path (str): The path to the directory
    - format (str): The format to compress to (7z, zip)
    - whitelist_ext (tuple[str]): A tuple of extensions to compress (e.g. (".png", ".jpg"))

    Returns
    - (bool): Whether compressing succeeded
    - (list[str]): A list of files that failed to compress
    """
    os.chdir(input_path)

    compress_cmd = ["7z", "a", "-bt", "-t" + format, "-mx1", "-r", os.path.join("../", output_path + "." + format)]
    if whitelist_ext[0] == "*":
        compress_cmd.append("*.*")
    else:
        compress_cmd += ["*" + i for i in whitelist_ext]
    cmd = subprocess.run(compress_cmd, capture_output=True)

    depth = len(os.path.normpath(input_path).split(os.sep))
    os.chdir(os.path.join("..", *([".."] * (depth - 1))))

    if cmd.returncode != 0:
        return input_path

    return ""


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
        print(
            BCOLORS.RED
            + "|=====["
            + BCOLORS.RESET
            + BCOLORS.BLUE
            + msg
            + BCOLORS.RESET
            + BCOLORS.RED
            + "]=====|"
            + BCOLORS.RESET
        )

    def __endswith(self, string: str, suffixes: list[str]) -> bool:
        for i in suffixes:
            if string.endswith(i):
                return True
        return False

    def __get_processable_folders(self) -> list[str]:
        """
        Get a list of folders that are processable
        - Does not exist <folder>.7z or <folder>.zip
        - Contains images
        - Does not exist <folder>_jxl || <folder>_avif || <folder>_upscaled
        """
        return_list: list[str] = []
        for i in os.listdir():
            if not os.path.isdir(i):
                continue
            if any([self.__endswith(i, [".7z", ".zip"]) for i in os.listdir(i)]):
                continue
            if not any([self.__endswith(i, [".png", ".jpg"]) for i in os.listdir(i)]):
                continue
            if any([self.__endswith(i, ["_jxl", "_avif", "_upscaled"]) for i in os.listdir(i)]):
                continue
            return_list.append(i)
        return return_list

    def __get_jxl_folders(self) -> list[str]:
        """
        Get a list of folders containing .jxl, .mp4, .webm, .gif
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

    def multiple_pack(self, packs: list[str] = [], reprocess: bool = False) -> None:
        if len(packs) == 0:
            packs = self.__get_processable_folders() if not reprocess else self.__get_jxl_folders()

        if len(packs) == 0:
            return

        if len(packs) > 1:
            print("These folders will be processed:")
            for i in packs:
                print(f"  {i}")

            if input("Continue? (y/n): ").lower() != "y":
                return

        for pack in packs:
            print()
            progress = f"{packs.index(pack) + 1}/{len(packs)}"
            self.__print_small_sign(f"{progress} | {pack}")
            images = list_files(pack, [".png", ".jpg"], True)

            png_dir = pack + "_png"
            os.makedirs(png_dir, exist_ok=True) if reprocess else None
            jxl_dir = pack + "_jxl"
            os.makedirs(jxl_dir, exist_ok=True) if not reprocess else None
            avif_dir = pack + "_avif"
            os.makedirs(avif_dir, exist_ok=True)
            mp4_dir = pack + "_mp4"
            os.makedirs(mp4_dir, exist_ok=True)
            upscaled_dir = pack + "_upscaled"
            os.makedirs(upscaled_dir, exist_ok=True)

            failed_png_transcode: list[str] = []
            if reprocess:
                print("👉 Transcoding images losslessly to PNG...")
                failed_png_transcode += batch_transcode(
                    list_files(pack, [".jxl"], True), png_dir, "png", Config.overwrite
                )[0]
                images = list_files(png_dir, [".png"], True)

            failed_jxl_transcode: list[str] = []
            if not reprocess:
                print("👉 Compressing images losslessly to JXL...")
                failed_jxl_transcode += batch_transcode(images, jxl_dir, "jxl", Config.overwrite)[0]

                print("👉 Copying animations into jxl folder...")
                for i in os.listdir(pack):
                    if not self.__endswith(i, [".mp4", ".gif", ".webm"]):
                        continue
                    if os.path.isfile(os.path.join(jxl_dir, i)):
                        continue
                    shutil.copy(os.path.join(pack, i), os.path.join(jxl_dir, i))

            print("👉 Resizing images if needed...")
            batch_resize(images, upscaled_dir, target_width=2500, target_height=0)

            print("👉 Transcoding resized images into .avif...")
            failed_avif_transcode = batch_transcode(
                list_files(upscaled_dir, [".png"], True), avif_dir, "avif", Config.overwrite, 1
            )[0]

            print("👉 Transcoding animations into .mp4 (av1)...")
            failed_mp4_transocde = batch_transcode(
                list_files(pack, [".mp4", ".gif", ".webm"], True), mp4_dir, "mp4", Config.overwrite, 1
            )[0]
            for i in os.listdir(mp4_dir):
                shutil.move(os.path.join(pack + "_mp4", i), os.path.join(pack + "_avif", i))
            shutil.rmtree(mp4_dir)

            if not reprocess:
                print("👉 Archiving .jxl, .gif, .mp4, .webm files into .7z...")
                failed_archive_jxl = single_compress(jxl_dir, pack, "7z", [".jxl", ".gif", ".mp4"])
                print(f"  Failed to archive {jxl_dir} to 7z") if failed_archive_jxl != "" else shutil.rmtree(jxl_dir)

            print("👉 Archiving .avif, .mp4 (av1) files into .zip...")
            failed_archive_avif = single_compress(avif_dir, pack, "zip", [".avif", ".mp4"])
            print(f"  Failed to archive {avif_dir} to zip") if failed_archive_avif != "" else shutil.rmtree(avif_dir)

            shutil.rmtree(png_dir) if reprocess else None
            message = f"{progress} | {pack}"
            if failed_png_transcode != []:
                message += f"\n  Failed transcode to PNG:\n    {failed_png_transcode}"
            if failed_jxl_transcode != []:
                message += f"\n  Failed transcode to JXL:\n    {failed_jxl_transcode}"
            if failed_avif_transcode != []:
                message += f"\n  Failed transcode to AVIF:\n    {failed_avif_transcode}"
            if failed_mp4_transocde != []:
                message += f"\n  Failed transcode to MP4:\n    {failed_mp4_transocde}"
            notify(progress, message)

    def one_pack(self, reprocess: bool = False):
        if reprocess:
            folders = self.__get_jxl_folders()
        else:
            folders = [i for i in os.listdir() if os.path.isdir(i)]
        if len(folders) == 0:
            return

        print(f"Select a folder to {'reprocess' if reprocess else 'process'}:")
        for i in range(len(folders)):
            print(f"  [{i}] {folders[i]}")
        selected_idx = input("Select a folder: ")
        if not selected_idx.isdigit() or int(selected_idx) not in range(len(folders)):
            return

        self.multiple_pack([folders[int(selected_idx)]], reprocess)


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
                menu.multiple_pack()
            case "2":
                menu.one_pack()
            case "3":
                menu.multiple_pack(reprocess=True)
            case "4":
                menu.one_pack(reprocess=True)
            case _:
                raise KeyboardInterrupt

        input("Press enter to continue...")


if __name__ == "__main__":
    for binary in ("ffmpeg", "ffprobe", "7z", "cjxl"):
        if shutil.which(binary) is None:
            print(f"Binary {binary} not found in PATH")
    try:
        main()
    except KeyboardInterrupt:
        print("\nExiting...")
        exit(0)
