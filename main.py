import math
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from textwrap import dedent
from zipfile import ZipFile

import requests

bool_env = {
    "logging": True,
    "overwrite": False,
}

str_env = {
    "webhook": "https://discord.com/api/webhooks/1130259221657694349/A4UwVh3JktN0cX2OKjJuzZ1lh8Ysjup6fYr_UvWkBoH26mDok_Ca0w21dVXh8MWaVFU3",
    "constant_msg_id": "1131964675874095105",
}


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
        str_env["webhook"],
        json={
            "embeds": [{"title": title, "description": description}],
            "username": username,
        },
    )


def update_discord_progress(stage_idx: int, pack: str, progress: str):
    stages = [
        "Preprocessing",
        "Transcoding to JXL",
        "Upscaling",
        "Compressing to AVIF",
        "Archiving to 7z",
        "Archiving to zip",
    ]

    # fmt: off
    requests.patch(
        str_env["webhook"] + "/messages/" + str_env["constant_msg_id"],
        json={
        "embeds": [{
            "title": f"{progress} | {pack}",
            "description": dedent(f"""\
                **Stage {stage_idx}/{len(stages)-1}**: {stages[stage_idx]}\
            """).strip()}]
    })
    # fmt: on


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


def get_dimension(path: str) -> tuple[int, int]:
    """
    Get the dimensions of an image

    Params
    - path (str): The path to the image

    Returns
    - (int, int): The width and height of the image, (-1, -1) if failed to get dimensions (e.g. file not found)
    """
    ffprobe_output = subprocess.run(
        f'ffprobe -v error -select_streams v:0 -show_entries stream=width,height -of csv=s=x:p=0 "{path}"',
        shell=True,
        capture_output=True,
    )
    if ffprobe_output.returncode != 0:
        return -1, -1

    width, height = ffprobe_output.stdout.decode("utf-8").split("x")
    width, height = int(width), int(height)
    return width, height


def batch_transcode(
    input_paths: list[str], format: str = "avif", overwrite: bool = False, threads: int = 4
) -> tuple[list[str], list[str]]:
    """
    Transcode a list of files to a specific format

    Params
    - input_paths (list[str]): A tuple of files to transcode
    - threads (int): Number of threads to use
    - format (str): The format to transcode to (avif, jxl, webp, png

    Returns
    - (list[str]): files that failed to transcode
    - (list[str]): files that skipped transcoding

    Notes
    - Output file/folder
        - If file is outside: <original_file_name>.<format>
        - If file inside (sub)folder(s): <parent>_<format>/.../<original_file_name>.<format>
    - Set threads to 1 to show progress
    """
    if len(input_paths) == 0:
        return [], []
    commands = {
        "avif": 'ffmpeg -i "{}" -c:v libsvtav1 -pix_fmt yuv420p10le -crf 24 -preset 6 -vf "scale=ceil(iw/2)*2:ceil(ih/2)*2"{} "{}"',
        "jxl": 'cjxl -q 100 -e 8 "{}" "{}"',
        "png": 'ffmpeg -i "{}" -c:v png -compression_level 6{} "{}"',
    }

    if format in ("avif", "mp4"):
        threads = 1

    output_paths: list[str] = []
    for path in input_paths:
        path_elements = norm(path).split("/")
        if len(path_elements) > 1:  # sits in a folder
            path_elements[0] += f"_{format}"
        path_elements[-1] = os.path.splitext(path_elements[-1])[0] + f".{format}"  # change extension
        output_path = "/".join(path_elements)
        if (out_dir := os.path.dirname(output_path)) and (not os.path.exists(out_dir)):  # create out_dir if not exists
            os.makedirs(out_dir)
        output_paths.append(output_path)

    overwrite_flag = " -y" if overwrite else ""

    def __helper(input_path: str, output_path: str) -> tuple[str, str]:
        """Run the transcoding command and return the status
        Returns
        - (str): The status of the transcoding (failed, skipped, or empty string if successful)
        - (str): The path of the input file
        """
        nonlocal overwrite_flag, format, threads
        if os.path.exists(output_path) and not overwrite:
            return "skipped", input_path

        cmd = commands[format]
        if commands[format].startswith("ffmpeg"):
            cmd = cmd.format(input_path, overwrite_flag, output_path)
        elif commands[format].startswith("cjxl"):
            cmd = cmd.format(input_path, output_path)

        if threads > 1:
            sp_output = open(f"transcode_{format}.log", "a") if bool_env["logging"] else subprocess.DEVNULL
        else:
            sp_output = None
            cmd = cmd.replace("ffmpeg", "ffpb")
        subprocess.run(
            cmd,
            shell=True,
            stdout=sp_output,
            stderr=sp_output,
        )
        if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            return "", ""
        return "failed", input_path

    return_data: dict[str, list[str]] = {
        "failed": [],
        "skipped": [],
        "": [],
    }
    if threads > 1:
        with ThreadPoolExecutor(max_workers=threads) as executor:
            futures = [executor.submit(__helper, i, o) for i, o in zip(input_paths, output_paths)]
            for future in as_completed(futures):
                status, path = future.result()
                return_data[status].append(path)
    else:
        for i, o in zip(input_paths, output_paths):
            status, path = __helper(i, o)
            return_data[status].append(path)

    return return_data["failed"], return_data["skipped"]


def single_upscale(
    input_path: str, output_path: str, width: int, height: int, target_width: int, target_height: int
) -> tuple[bool, str]:
    """
    Upscale a single image

    Params
    - input_path (str): The path to the input image
    - output_path (str): The path to the output image
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
        f'ffprobe -v error -select_streams v:0 -show_entries stream=width,height -of csv=s=x:p=0 "{input_path}"',
        shell=True,
        capture_output=True,
    )
    if ffprobe_output.returncode != 0:
        return False, ffprobe_output.stderr.decode("utf-8")

    width_only = target_width != 0 and target_height == 0
    height_only = target_width == 0 and target_height != 0

    # ----- Calculate scale and upscale ----- #
    scale = 0
    if width_only:
        scale = math.ceil(target_width / width)
    elif height_only:
        scale = math.ceil(target_height / height)
    else:
        scale = max(math.ceil(target_width / width), math.ceil(target_height / height))

    model = "realesrgan-x4plus-anime" if scale == 4 else "realesr-animevideov3"
    subprocess.run(
        f'realesrgan-ncnn-vulkan -i "{input_path}" -o "{output_path}" -s {scale} -n {model} -f png',
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
        log_file = open("upscale.log", "a") if bool_env["logging"] else subprocess.DEVNULL
        subprocess.run(
            f'ffmpeg -i "{output_path}" -vf "{filter}" -y "{output_path}.png"',  # <output_path>.png.png
            shell=True,
            stdout=log_file,
            stderr=log_file,
        )
        os.remove(output_path)
        os.rename(f"{output_path}.png", output_path)

    if os.path.exists(output_path):
        return True, input_path
    return False, "Downscaling after upscaling failed"


def batch_resize(
    input_paths: list[str], threads: int = 4, target_width: int = 0, target_height: int = 0
) -> tuple[list[str], str]:
    """
    Upscale a list of images

    Params
    - input_paths (list[str]): A list of images to upscale
    - threads (int): Number of threads to use
    - target_width (int): The target width
    - target_height (int): The target height

    Returns
    - (list[str]): A list of images that failed to upscale
    - (str): The output folder

    Notes
    - Output file/folder
        - If file is outside: <original_file_name>_upscaled.png
        - If file inside (sub)folder(s): <parent>_upscaled/.../<original_file_name>_upscaled.png
    """
    if input_paths == []:
        return []
    output_paths: list[str] = []
    for path in input_paths:
        path_elements = norm(path).split("/")
        if len(path_elements) > 1:  # sits in a folder
            path_elements[0] += "_upscaled"
        path_elements[-1] = os.path.splitext(path_elements[-1])[0] + ".png"  # change extension
        output_path = "/".join(path_elements)
        if (out_dir := os.path.dirname(output_path)) and (not os.path.exists(out_dir)):  # create out_dir if not exists
            os.makedirs(out_dir)
        output_paths.append(output_path)

    def __helper(input_path: str, output_path: str):
        nonlocal target_width, target_height
        if os.path.exists(output_path) and not bool_env["overwrite"]:
            return True, ""
        width, height = get_dimension(input_path)

        if width == -1 or height == -1:
            return False, input_path

        if target_width != 0 and width >= target_width:
            # use ffmpeg to downscale
            log_file = open("downscale.log", "a") if bool_env["logging"] else subprocess.DEVNULL
            subprocess.run(
                f'ffmpeg -i "{input_path}" -vf "scale={target_width}:-1" -y "{output_path}"',
                shell=True,
                stdout=log_file,
                stderr=log_file,
            )
            if os.path.exists(output_path):
                return True, ""
            return False, input_path

        if target_height != 0 and height >= target_height:
            # use ffmpeg to downscale
            log_file = open("downscale.log", "a") if bool_env["logging"] else subprocess.DEVNULL
            subprocess.run(
                f'ffmpeg -i "{input_path}" -vf "scale=-1:{target_height}" -y "{output_path}"',
                shell=True,
                stdout=log_file,
                stderr=log_file,
            )
            if os.path.exists(output_path):
                return True, ""
            return False, input_path

        return single_upscale(input_path, output_path, width, height, target_width, target_height)

    failed: list[str] = []
    with ThreadPoolExecutor(max_workers=threads) as executor:
        futures = [executor.submit(__helper, i, o) for i, o in zip(input_paths, output_paths)]
        for future in as_completed(futures):
            if not future.result()[0]:
                failed.append(future.result()[1])

    return failed, os.path.dirname(output_paths[0])


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


def batch_calculate_blurhash(input_zips: list[str], threads: int = 16) -> tuple[list[str], dict[str, dict[str, list[str]]]]:
    """
    Calculate blurhash for all images in a list of zip files

    Params
    - input_zips (list[str]): A list of zip files to calculate blurhash
    - threads (int): Number of threads to use

    Returns
    - (list[str]): A list of images that failed to calculate blurhash
    """

    failed: list[str] = []
    final_data: dict[str, dict[str, list[str]]] = {}
    # {
    #     "path/to/zip": {
    #         "image1": [<blurhash>, <width>, <height>],
    #         "image2": [<blurhash>, <width>, <height>],
    #     }
    #     "path/to/zip2": {
    #         "image1": [<blurhash>, <width>, <height>],
    #         "image2": [<blurhash>, <width>, <height>],
    #     }
    # }

    def __helper(file: str, temp_path: str, zip_file: str) -> tuple[str, str, list[str]]:
        if not file.endswith((".png", ".jpg", ".jpeg", ".avif", ".jxl")):
            return "skipped", file.replace(temp_path, zip_file), ["", "", ""]
        width, height = get_dimension(file)
        blur_hash = subprocess.run(
            f'blurhash-cli "{file}"',
            shell=True,
            capture_output=True,
        )
        if blur_hash.returncode != 0:
            return "failed", file.replace(temp_path, zip_file), ["", "", ""]
        blur_hash = blur_hash.stdout.decode("utf-8").strip()
        return "success", file.replace(temp_path, zip_file), [blur_hash, str(width), str(height)]

    temp_path = "blurhash_temp"
    for zip_path in input_zips:
        with ZipFile(zip_path, "r") as zip_file:
            zip_file.extractall(path=temp_path)
            files = list_files(temp_path, [".png", ".jpg", ".jpeg", ".avif", ".jxl"], True)
            with ThreadPoolExecutor(max_workers=threads) as executor:
                futures = [executor.submit(__helper, file, temp_path, zip_path) for file in files]
                for future in as_completed(futures):
                    status, file, data = future.result()
                    match status:
                        case "failed":
                            failed.append(os.path.join(zip_path, data[0]))
                            continue
                        case "success":
                            if zip_path not in final_data:
                                final_data[zip_path] = {}
                            final_data[zip_path][file] = data
                        case _:
                            continue
        shutil.rmtree(temp_path)

    return failed, final_data


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
        Get a list of folders containing JXL images
        """
        return_list: list[str] = []
        for i in os.listdir():
            if not os.path.isdir(i):
                continue
            if not any([self.__endswith(i, [".jxl"]) for i in os.listdir(i)]):
                continue
            return_list.append(i)
        return return_list

    def __handle_error(self, msg_command: str, failed_list: list[str], skipped_list: list[str]) -> None:
        if len(msg_command.split("_")) != 2:
            raise ValueError("msg_command must be in the format of <command>_<image format>")
        msg_type, format = msg_command.split("_")
        msg = ""
        match msg_type:
            case "transcode":
                msg = f"Failed to transcode the following images to {format}:"
            case "resize":
                msg = "Failed to resize the following images:"
            case _:
                raise ValueError("msg_type must be one of transcode, resize")

        if len(failed_list) > 0:
            print("  " + msg)
            for i in failed_list:
                print(f"    {i}")
        if len(skipped_list) > 0:
            print("  Skipped the following images:")
            for i in skipped_list:
                print(f"    {i}")

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

        metadata: dict[str, dict[str, list[str]]] = {}

        for pack in packs:
            print()
            progress = f"{packs.index(pack) + 1}/{len(packs)}"
            self.__print_small_sign(f"{progress} 【{pack}】")
            images = list_files(pack, [".png", ".jpg"], True)

            if reprocess:
                print("👉 Transcoding images losslessly to PNG...")
                update_discord_progress(0, pack, progress)
                failed_2_png, skipped_2_png = batch_transcode(list_files(pack, [".jxl"], True), "png", bool_env["overwrite"])
                self.__handle_error("transcode_png", failed_2_png, skipped_2_png)
                images = list_files(pack + "_png", [".png"], True)

            failed_2_jxl, skipped_2_jxl = [], []
            if not reprocess:
                print("👉 Compressing images losslessly to JXL...")
                update_discord_progress(1, pack, progress)
                failed_2_jxl, skipped_2_jxl = batch_transcode(images, "jxl", bool_env["overwrite"])
                self.__handle_error("transcode_jxl", failed_2_jxl, skipped_2_jxl)

            print("👉 Resizing images if needed...")
            update_discord_progress(2, pack, progress)
            failed_2_resize, upscaled_dir = batch_resize(images, target_width=2500, target_height=0)
            self.__handle_error("resize_", failed_2_resize, [])

            print("👉 Compressing resized images to AVIF...")
            update_discord_progress(3, pack, progress)
            failed_2_avif, skipped_2_avif = batch_transcode(
                list_files(upscaled_dir, [".png"], True), "avif", bool_env["overwrite"], 1
            )
            self.__handle_error("transcode_avif", failed_2_avif, skipped_2_avif)

            if not reprocess:
                print("👉 Archiving JXL images to 7z...")
                jxl_dir = pack + "_jxl"
                update_discord_progress(4, pack, progress)
                failed_archive_jxl = single_compress(jxl_dir, pack, "7z", [".jxl", ".gif", ".mp4"])
                print(f"  Failed to archive {jxl_dir} to 7z") if failed_archive_jxl != "" else shutil.rmtree(jxl_dir)

            print("👉 Archiving AVIF images to zip...")
            avif_dir = upscaled_dir + "_avif"
            update_discord_progress(5, pack, progress)
            failed_archive_avif = single_compress(avif_dir, pack, "zip", [".avif"])
            print(f"  Failed to archive {avif_dir} to zip") if failed_archive_avif != "" else shutil.rmtree(avif_dir)

            notify(pack, f"{progress}【{pack}】finished processing")

        os.remove("metadata.json") if os.path.exists("metadata.json") else None
        with open("metadata.json", "w", encoding="utf-8") as f:
            f.write(str(metadata))

        notify("Finished processing", "Finished processing all packs")

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

    def re_calculate_blurhash(self):
        zip_files = list_files(".", [".zip"], True)
        if len(zip_files) == 0:
            return

        print("These zip files will be processed:")
        for i in zip_files:
            print(f"  {i}")

        if input("Continue? (y/n): ").lower() != "y":
            return

        falied_blurhash, blurhash_data = batch_calculate_blurhash(zip_files)
        if len(falied_blurhash) > 0:
            print("  Failed to calculate blurhash for the following images:")
            for i in falied_blurhash:
                print(f"    {i}")
        with open("metadata.json", "w", encoding="utf-8") as f:
            f.write(str(blurhash_data))

        notify("Re-calculated blurhash", "Re-calculated blurhash for all images")


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
                5: "Re-calculate blurhash",
                "div3": "",
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
            case "5":
                menu.re_calculate_blurhash()
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
