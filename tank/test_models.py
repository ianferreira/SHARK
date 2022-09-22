from shark.iree_utils._common import (
    check_device_drivers,
    device_driver_info,
    IREE_DEVICE_MAP,
)
from shark.iree_utils.vulkan_utils import get_vulkan_triple_flag
from parameterized import parameterized
from shark.shark_downloader import (
    download_tf_model,
    download_torch_model,
    download_tflite_model,
)
from shark.shark_inference import SharkInference
from shark.parser import shark_args
import iree.compiler as ireec
import pytest
import unittest
import numpy as np
import csv
import tempfile
import os
import shutil


def load_csv_and_convert(filename, gen=False):
    """
    takes in a csv filename and generates a dict for consumption by get_valid_test_params
    """
    model_configs = []
    with open(filename, "r+") as f:
        reader = csv.reader(f, delimiter=",")
        for row in reader:
            if len(row) < 5:
                print("invalid model: " + row)
                continue
            model_configs.append(
                {
                    "model_name": row[0],
                    "dialect": row[1],
                    "framework": row[2],
                    "rtol": float(row[3]),
                    "atol": float(row[4]),
                    "out_type": row[5],
                }
            )
    # This is a pytest workaround
    if gen:
        with open("tank/dict_configs.py", "w+") as out:
            out.write("ALL = [\n")
            for c in model_configs:
                out.write(str(c) + ",\n")
            out.write("]")
    return model_configs


def get_valid_test_params():
    """
    Generate a list of all combinations of available devices and static/dynamic flag.
    """
    device_list = [
        device
        for device in IREE_DEVICE_MAP.keys()
        if not check_device_drivers(device)
    ]
    dynamic_list = (True, False)
    # TODO: This is soooo ugly, but for some reason creating the dict at runtime
    # results in strange pytest failures.
    load_csv_and_convert("tank/all_models.csv", True)
    from tank.dict_configs import ALL

    config_list = ALL

    param_list = [
        (dynamic, device, config)
        for dynamic in dynamic_list
        for device in device_list
        for config in config_list
    ]

    filtered_param_list = [
        params for params in param_list if is_valid_case(params)
    ]

    return filtered_param_list


def is_valid_case(test_params):
    if test_params[0] == True and test_params[2]["framework"] == "tf":
        return False
    else:
        return True


def shark_test_name_func(testcase_func, param_num, param):
    """
    Generate function name string which shows dynamic/static and device name.
    this will be ingested by 'parameterized' package to rename the pytest.
    """
    param_names = []
    for x in param.args:
        if x == True:
            param_names.append("dynamic")
        elif x == False:
            param_names.append("static")
        elif "model" in str(x):
            as_list = str(x).split(" ")
            as_list = [
                parameterized.to_safe_name(x).strip("_") for x in as_list
            ]
            param_names.insert(0, as_list[as_list.index("model_name") + 1])
            param_names.insert(1, as_list[as_list.index("framework") + 1])
            # param_names.append(as_list[3])

        else:
            param_names.append(x)
    return "%s_%s" % (
        testcase_func.__name__,
        parameterized.to_safe_name("_".join(str(x) for x in param_names)),
    )


class SharkModuleTester:
    def __init__(self, config):
        """config should be a dict containing minimally:
        dialect: (str) name of input dialect
        framework: (str) one of tf, tflite, pytorch
        model_name: (str) name of the model in the tank ("resnet50")
        rtol/atol: (float) tolerances for golden values
        """
        self.config = config

    def create_and_check_module(self, dynamic, device):
        if self.config["framework"] == "tf":
            model, func_name, inputs, golden_out = download_tf_model(
                self.config["model_name"]
            )
        elif self.config["framework"] == "torch":
            model, func_name, inputs, golden_out = download_torch_model(
                self.config["model_name"]
            )
        elif self.config["framework"] == "tflite":
            model, func_name, inputs, golden_out = download_tflite_model(
                model_name=self.config["model_name"]
            )
        else:
            model, func_name, inputs, golden_out = None, None, None, None

        shark_module = SharkInference(
            model,
            func_name,
            device=device,
            mlir_dialect=self.config["dialect"],
            is_benchmark=self.benchmark,
        )

        try:
            shark_module.compile()
        except:
            if any([self.ci, self.save_repro, self.save_fails]) == True:
                self.save_reproducers()
            if self.ci == True:
                self.upload_repro(self.ci_sha)
            raise

        result = shark_module.forward(inputs)
        golden_out, result = self.postprocess_outputs(golden_out, result)
        try:
            np.testing.assert_allclose(
                golden_out,
                result,
                rtol=self.config["rtol"],
                atol=self.config["atol"],
            )
        except AssertionError:
            if any([self.ci, self.save_repro, self.save_fails]) == True:
                self.save_reproducers()
            if self.ci == True:
                self.upload_repro()
            if self.benchmark == True:
                self.benchmark_module(shark_module, inputs, dynamic, device)
            raise

        if self.benchmark == True:
            self.benchmark_module(shark_module, inputs, dynamic, device)

        if self.save_repro == True:
            self.save_reproducers()

    def benchmark_module(self, shark_module, inputs, dynamic, device):
        shark_args.enable_tf32 = self.tf32
        if shark_args.enable_tf32 == True:
            shark_module.compile()
            shark_args.enable_tf32 = False

        shark_args.onnx_bench = self.onnx_bench
        shark_module.shark_runner.benchmark_all_csv(
            (inputs),
            self.config["model_name"],
            dynamic,
            device,
            self.config["framework"],
        )

    def save_reproducers(self):
        # Saves contents of IREE TempFileSaver temporary directory to ./shark_tmp/saved/<test_case>.
        src = self.temp_dir
        trg = f"./shark_tmp/saved/{self.tmp_prefix}"
        if not os.path.isdir("./shark_tmp/saved/"):
            os.mkdir("./shark_tmp/saved/")
        if not os.path.isdir(trg):
            os.mkdir(trg)
        files = os.listdir(src)
        for fname in files:
            shutil.copy2(os.path.join(src, fname), trg)

    def upload_repro(self):
        import subprocess

        bashCommand = f"gsutil cp -r ./shark_tmp/saved/{self.tmp_prefix}/* gs://shark-public/builder/repro_artifacts/"
        process = subprocess.run(bashCommand.split())

    def postprocess_outputs(self, golden_out, result):
        # Prepares result tensors of forward pass and golden values for comparison, when needed.
        if self.config["out_type"] == "tf_vit":
            ir_device_array = result[0][1]
            logits = ir_device_array.astype(ir_device_array.dtype)
            logits = np.squeeze(logits, axis=0)
            expected = golden_out[0]
        elif self.config["out_type"] == "tf_hf":
            logits = result[0][1].to_host()
            expected = golden_out
        elif self.config["out_type"] == "default":
            logits = result
            expected = golden_out

        return expected, logits


class SharkModuleTest(unittest.TestCase):
    @pytest.fixture(autouse=True)
    def configure(self, pytestconfig):
        self.pytestconfig = pytestconfig

    param_list = get_valid_test_params()

    @parameterized.expand(param_list, name_func=shark_test_name_func)
    def test_module(self, dynamic, device, config):
        self.module_tester = SharkModuleTester(config)
        self.module_tester.benchmark = self.pytestconfig.getoption("benchmark")
        self.module_tester.save_repro = self.pytestconfig.getoption(
            "save_repro"
        )
        self.module_tester.save_fails = self.pytestconfig.getoption(
            "save_fails"
        )
        self.module_tester.onnx_bench = self.pytestconfig.getoption(
            "onnx_bench"
        )
        self.module_tester.tf32 = self.pytestconfig.getoption("tf32")
        self.module_tester.ci = self.pytestconfig.getoption("ci")
        self.module_tester.ci_sha = self.pytestconfig.getoption("ci_sha")
        if (
            config["model_name"] == "distilbert-base-uncased" 
            and config["framework"] == "torch"
        ):
            pytest.xfail(reason="https://github.com/nod-ai/SHARK/issues/354")
        if (
            config["model_name"] == "facebook/convnext-tiny-224"
            and device == "cuda"
        ):
            pytest.xfail(reason="https://github.com/nod-ai/SHARK/issues/311")
        if (
            config["model_name"] == "google/vit-base-patch16-224"
            and device == "cuda"
        ):
            pytest.xfail(reason="https://github.com/nod-ai/SHARK/issues/311")
        if config["model_name"] == "resnet50" and device in [
            "metal",
            "vulkan",
        ]:
            if get_vulkan_triple_flag() is not None:
                if "m1-moltenvk-macos" in get_vulkan_triple_flag():
                    pytest.xfail(
                        reason="M2: Assert Error & M1: CompilerToolError"
                    )
        if config["model_name"] == "roberta-base" and device == "cuda":
            pytest.xfail(reason="https://github.com/nod-ai/SHARK/issues/274")
        if config["model_name"] == "google/rembert":
            pytest.skip(reason="Model too large to convert.")
        if config[
            "model_name"
        ] == "dbmdz/convbert-base-turkish-cased" and device in [
            "metal",
            "vulkan",
        ]:
            pytest.xfail(
                reason="Issue: https://github.com/iree-org/iree/issues/9971"
            )
        if config["model_name"] == "facebook/convnext-tiny-224" and device in [
            "cuda",
            "metal",
            "vulkan",
        ]:
            pytest.xfail(
                reason="https://github.com/nod-ai/SHARK/issues/311, https://github.com/nod-ai/SHARK/issues/342"
            )
        if config["model_name"] == "funnel-transformer/small" and device in [
            "cuda",
            "metal",
            "vulkan",
        ]:
            pytest.xfail(
                reason="failing in the iree-compiler passes, see https://github.com/nod-ai/SHARK/issues/201"
            )
        if (
            config["model_name"] == "google/vit-base-patch16-224"
            and device == "cuda"
        ):
            pytest.xfail(reason="https://github.com/nod-ai/SHARK/issues/311")
        if config["model_name"] == "microsoft/mpnet-base":
            pytest.xfail(reason="https://github.com/nod-ai/SHARK/issues/203")
        if config["model_name"] == "nvidia/mit-b0":
            pytest.xfail(reason="https://github.com/nod-ai/SHARK/issues/343")
        if (
            config["model_name"] == "google/mobilebert-uncased"
            and device in ["metal", "vulkan"]
            and config["framework"] == "torch"
        ):
            pytest.xfail(
                reason="Numerics issues -- https://github.com/nod-ai/SHARK/issues/344"
            )
        if config["framework"] == "tf" and dynamic == True:
            pytest.skip(
                reason="Dynamic shapes not supported for this framework."
            )

        safe_name = (
            f"{config['model_name']}_{config['framework']}_{dynamic}_{device}"
        )
        self.module_tester.tmp_prefix = safe_name.replace("/", "_")

        if not os.path.isdir("./shark_tmp/"):
            os.mkdir("./shark_tmp/")

        tempdir = tempfile.TemporaryDirectory(
            prefix=self.module_tester.tmp_prefix, dir="./shark_tmp/"
        )
        self.module_tester.temp_dir = tempdir.name

        with ireec.tools.TempFileSaver(tempdir.name):
            self.module_tester.create_and_check_module(dynamic, device)
