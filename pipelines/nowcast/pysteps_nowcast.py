import argparse


def main(region: str):
    print(f"Nowcast pipeline stub running for {region}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", default="kununurra")
    args = parser.parse_args()
    main(args.region)
