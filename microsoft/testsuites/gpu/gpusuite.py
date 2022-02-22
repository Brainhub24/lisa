# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

import re
from pathlib import Path

from assertpy import assert_that

from lisa import (
    LisaException,
    Logger,
    Node,
    SkippedException,
    TestCaseMetadata,
    TestSuite,
    TestSuiteMetadata,
    constants,
    simple_requirement,
)
from lisa.features import Gpu, SerialConsole
from lisa.features.gpu import ComputeSDK
from lisa.operating_system import Debian
from lisa.tools import Lspci, NvidiaSmi, Pip, Python, Reboot, Tar, Wget
from lisa.util import get_matched_str

_cudnn_location = (
    "https://partnerpipelineshare.blob.core.windows.net/"
    "packages/cudnn-10.0-linux-x64-v7.5.0.56.tgz"
)
_cudnn_file_name = "cudnn.tgz"


@TestSuiteMetadata(
    area="gpu",
    category="functional",
    name="Gpu",
    description="""
    This test suite runs the gpu test cases.
    """,
)
class GpuTestSuite(TestSuite):
    TIMEOUT = 2000

    _pytorch_pattern = re.compile(r"^gpu count: (?P<count>\d+)", re.M)

    @TestCaseMetadata(
        description="""
            This test case verifies if gpu drivers are loaded fine.

            Steps:
            1. Validate if the VM SKU is supported for GPU.
            2. Install LIS drivers if not already installed for Fedora and its
                derived distros. Reboot the node
            3. Install required gpu drivers on the VM and reboot the node. Validate gpu
                drivers can be loaded successfully.

        """,
        timeout=TIMEOUT,
        requirement=simple_requirement(
            supported_features=[Gpu, SerialConsole],
        ),
        priority=1,
    )
    def verify_load_gpu_driver(self, node: Node, log_path: Path, log: Logger) -> None:
        _check_driver_installed(node)

    @TestCaseMetadata(
        description="""
            This test case verifies the gpu adapter count.

            Steps:
            1. Assert that node supports GPU.
            2. If GPU modules are not loaded, install and load the module first.
            3. Find the expected gpu count for the node.
            4. Validate expected and actual gpu count using lsvmbus output.
            5. Validate expected and actual gpu count using lspci output.
            6. Validate expected and actual gpu count using gpu vendor commands
                 example - nvidia-smi
        """,
        timeout=TIMEOUT,
        requirement=simple_requirement(
            supported_features=[Gpu],
        ),
        priority=2,
    )
    def verify_gpu_adapter_count(self, node: Node, log_path: Path, log: Logger) -> None:
        _check_driver_installed(node)

        gpu_feature = node.features[Gpu]
        assert isinstance(node.capability.gpu_count, int)
        expected_count = node.capability.gpu_count

        lsvmbus_device_count = gpu_feature.get_gpu_count_with_lsvmbus()
        assert_that(
            lsvmbus_device_count,
            "Expected device count didn't match Actual device count from lsvmbus",
        ).is_equal_to(expected_count)

        lspci_device_count = gpu_feature.get_gpu_count_with_lspci()
        assert_that(
            lspci_device_count,
            "Expected device count didn't match Actual device count from lspci",
        ).is_equal_to(expected_count)

        _check_driver_installed(node)

        vendor_cmd_device_count = gpu_feature.get_gpu_count_with_vendor_cmd()
        assert_that(
            vendor_cmd_device_count,
            "Expected device count didn't match Actual device count"
            " from vendor command",
        ).is_equal_to(expected_count)

    @TestCaseMetadata(
        description="""
        This test case will
        1. Validate disabling GPU devices.
        2. Validate enable back the disabled GPU devices.
        """,
        priority=2,
        requirement=simple_requirement(
            supported_features=[Gpu],
        ),
    )
    def verify_gpu_rescind_validation(self, node: Node) -> None:
        _check_driver_installed(node)

        lspci = node.tools[Lspci]
        gpu = node.features[Gpu]

        # 1. Disable GPU devices.
        gpu_devices = lspci.get_devices_by_type(device_type=constants.DEVICE_TYPE_GPU)
        gpu_devices = gpu.remove_virtual_gpus(gpu_devices)

        for device in gpu_devices:
            lspci.disable_device(device)

        # 2. Enable GPU devices.
        lspci.enable_devices()

    @TestCaseMetadata(
        description="""
        This test case will run PyTorch to check CUDA driver installed correctly.

        1. Install PyTorch.
        2. Check GPU count by torch.cuda.device_count()
        3. Compare with PCI result
        """,
        priority=3,
        requirement=simple_requirement(
            supported_features=[Gpu],
        ),
    )
    def verify_gpu_cuda_with_pytorch(self, node: Node) -> None:
        _check_driver_installed(node)

        _install_cudnn(node)

        gpu = node.features[Gpu]

        pip = node.tools[Pip]
        if not pip.exists_package("torch"):
            pip.install_packages("torch")

        gpu_script = "import torch;print(f'gpu count: {torch.cuda.device_count()}')"
        python = node.tools[Python]
        expected_count = gpu.get_gpu_count_with_lspci()

        script_result = python.run(
            f'-c "{gpu_script}"',
            force_run=True,
        )
        gpu_count_str = get_matched_str(script_result.stdout, self._pytorch_pattern)
        script_result.assert_exit_code(
            message=f"failed on run gpu script: {gpu_script}, "
            f"output: {script_result.stdout}"
        )

        assert_that(gpu_count_str).described_as(
            f"gpu count is not in result: {script_result.stdout}"
        ).is_not_empty()

        gpu_count = int(gpu_count_str)
        assert_that(gpu_count).described_as(
            "GPU must be greater than zero."
        ).is_greater_than(0)
        assert_that(gpu_count).described_as(
            "cannot detect GPU from PyTorch"
        ).is_equal_to(expected_count)


def _check_driver_installed(node: Node) -> None:
    gpu = node.features[Gpu]

    if not gpu.is_supported():
        raise SkippedException(f"GPU is not supported with distro {node.os.name}")
    if ComputeSDK.AMD in gpu.get_supported_driver():
        raise SkippedException("AMD vm sizes is not supported")
    try:
        _ = node.tools[NvidiaSmi]
    except Exception as identifier:
        raise LisaException(
            f"Cannot find nvidia-smi, make sure the driver installed correctly. "
            f"Inner exception: {identifier}"
        )


def _install_cudnn(node: Node) -> None:
    wget = node.tools[Wget]
    tar = node.tools[Tar]

    path = wget.get_tool_path(use_global=True)
    extracted_path = tar.get_tool_path(use_global=True)
    if node.shell.exists(path / _cudnn_file_name):
        return

    download_path = wget.get(
        url=_cudnn_location, filename=str(_cudnn_file_name), file_path=str(path)
    )
    tar.extract(download_path, dest_dir=str(extracted_path))
    if isinstance(node.os, Debian):
        target_path = "/usr/lib/x86_64-linux-gnu/"
    else:
        target_path = "/usr/lib64/"
    node.execute(
        f"cp -p {extracted_path}/cuda/lib64/libcudnn* {target_path}",
        shell=True,
        sudo=True,
    )
    return


# We use platform to install the driver by default. If in future, it needs to
# install independently, this logic can be reused.
def _ensure_driver_installed(
    node: Node, gpu_feature: Gpu, log_path: Path, log: Logger
) -> None:
    if gpu_feature.is_module_loaded():
        return

    gpu_feature.install_compute_sdk()
    log.debug(
        f"{gpu_feature.get_supported_driver()} sdk installed. "
        "Will reboot to load driver."
    )

    reboot_tool = node.tools[Reboot]
    reboot_tool.reboot_and_check_panic(log_path)

    _check_driver_installed(node)
