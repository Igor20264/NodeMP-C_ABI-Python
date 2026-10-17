import os

import nodemp.mojo as mojo


def show(result, error):
    if error:
        node.log_error(error)
    else:
        node.log_info(f"mojo triangle = {result}")


kernel = mojo.load_extension(os.path.join(node.resource_dir, "kernel.pyd"))
node.job(lambda: kernel.triangle(1000), done=show)
