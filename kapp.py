from kubeauto.cli import KubeautoCLI

def main():
    """Entry point for the CLI"""
    cli = KubeautoCLI()
    cli.run()


if __name__ == "__main__":
    main()