import json
import time
import itertools
import base64
import os
from io import BytesIO
from typing import Optional, Tuple, List, Dict
import re
import requests


import boto3
from botocore.exceptions import ClientError

import pymupdf4llm as mde


S3_BUCKET_NAME = "" # Your bucket name here
AWS_REGION = "" # Your AWS region here
BEDROCK_MODEL_ID = "us.amazon.nova-micro-v1:0" # "us.anthropic.claude-3-5-sonnet-20241022-v2:0" 


SYSTEM_PROMPT = """You are an experienced scientist who is also an expert editor at major scientific journals. \
You understand research papers in a wide variety of subject matter, not only in terms of \
strucutre, style, and scientific quality of content, but also in terms of commercial viability and usefulness."""

SYSTEM_PROMPT_FOR_REFERENCES_EXCTRACTION = """\
### ROLE
You are a precision Metadata Extraction Assistant specializing in academic bibliographies.

### OBJECTIVE
Your sole task is to extract every academic reference found within the <text_to_parse> tags in the User Message. \
You must identify ALL references and group them into a single call using the submit_bibliography tool. \
Before submitting, verify that every reference in the source text has a corresponding entry in your list.

### EXTRACTION LOGIC
- **Authors:** Follow the naming convention of the bibliography. Extract all explicitly named authors. IMPORTANT: Do not include the phrase 'et al.'; only capture the names provided.
- **Title:** Capture the full, exact title. MAINTAIN all LaTeX mathematical notation (e.g., $...$ or \\...).
- **Date:** Extract the publication year only as an integer. If no date is present write 0.
- **arXiv ID:** Extract the numerical identifier only (e.g., 2301.12345). If the ID is within a URL, strip the URL prefix.

### OPERATIONAL RULES
1. **Completeness:** Do not stop after the first few references. Scan the entire document through page breaks, headers, and noise until the end.
2. **Missing Data:** If a field is not present in the text, return 'null' for that field.
3. **No Conversation:** Do not provide any introductory or concluding text. Your output must only be the tool call.
4. **Resilience:** Ignore non-bibliographic text noise, page numbers, page breaks, and unrelated headers between or around references.
"""

SYSTEM_PROMPT_FOR_COMPARISON_OF_REFERENCES = """\

You are an expert at comparing academic paper titles, authors, and publication dates. You are to assess whether the data provided for \
two separate publications generally match. The titles should be very, very close if not exact matches. In addition, the authors should be very, very \
close, though the order and number may be slightly different. The dates of publication should match but also could have a one \
year discrepency at most. You are to use your best judgement with the aforementioned in mind to decide if these two publications that you are \
provided are the same."""

TOOL_LIST = [
    {
        "toolSpec": {
            "name": "submit_bibliography",
            "description": "Extracts all academic references into a single structured list.",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "references_list": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "authors": {
                                        "type": "string",
                                        "description": "Follow the naming convention of the bibliography. Extract all explicitly named authors. Do not include the phrase 'et al.'; only capture the names provided.",
                                    },
                                    "title": {
                                        "type": "string",
                                        "description": "Capture the full, exact title. MAINTAIN all LaTeX mathematical notation (e.g., $...$ or \\...).",
                                    },
                                    "year": {
                                        "type": "integer",
                                        "description": "The Four-digit year only.",
                                    },
                                    "arxiv_id": {
                                        "type": "string",
                                        "description": "The numerical identifier only (e.g., 2301.12345). If the ID is within a URL, strip the URL prefix",
                                    },
                                },
                                "required": ["authors", "title"],
                            },
                        }
                    },
                    "required": ["references_list"],
                }
            },
        }
    }
]



def lambda_handler(event, context):
    """Main Lambda handler function"""
    print(f"Received event: {json.dumps(event)}")
    print("Entered handler\n")
    # Handle CORS preflight OPTIONS request
    if event.get("requestContext", {}).get("http", {}).get("method") == "OPTIONS":
        return create_response(200, {"message": "CORS preflight"})

    try:
        # Parse request body
        if isinstance(event.get("body"), str):
            body = json.loads(event["body"])
        else:
            body = event.get("body", {})

        # Validate required parameters
        validation_error = validate_request(body)
        if validation_error:
            return create_response(
                400,
                {
                    "success": False,
                    "error": validation_error,
                    "errorCode": "MISSING_PARAMETERS",
                    "thingy": f"\n\n{body}\n\n",
                },
            )

        credentials = body["credentials"]
        file_content = base64.b64decode(body["fileBase64"])
        prompt = body["prompt"]
        file_name = body["fileName"]
        references_title = body.get("referencesTitle", "")
        run_references_check = body.get("runReferencesCheck", False)

        # Validate credentials
        if not validate_credentials(credentials):
            return create_response(
                401,
                {
                    "success": False,
                    "error": "Invalid AWS credentials",
                    "errorCode": "INVALID_CREDENTIALS",
                },
            )

        # Ensure S3 bucket exists with lifecycle policy
        ensure_bucket_exists(credentials)

        # Upload file to S3
        s3_keys = upload_file_to_s3(credentials, file_content, file_name)
        print(f"File uploaded to S3: {list(s3_keys.values())}")

        # Retrieve file content from S3 (verification step)
        retrieved_content = get_markdown_file_from_s3(
            credentials, s3_keys["markdown_s3_key"]
        )
        print("File retrieved from S3 successfully pre bedrock call")

        # Process with Bedrock
        # Uses Converse API (higher level, conveniences for multi-round chat and same interface across models)
        bedrock_result = process_with_bedrock_converse(
            credentials, prompt, retrieved_content
        )
        print("Bedrock procesing of paper is complete.")

        references_validation_results = []
        references_lookup_errors = []
        if references_title and references_title.strip():
            print(f"Extracting references from {references_title} section.")
            extract_and_upload_references(
                retrieved_content, file_name, references_title, credentials
            )
            print("Bedrock references processing complete.")

            # Run validation if requested
            if run_references_check:
                print("Running LLM-assisted reference validation.")
                references_validation_results, references_lookup_errors = validate_references(
                    file_name, references_title, credentials
                )
                print(f"Reference validation complete. Found {len(references_validation_results)} mismatches, {len(references_lookup_errors)} lookup errors.")

        return create_response(
            200, {
                "success": True,
                "result": bedrock_result,
                "s3Key": s3_keys,
                "referencesValidation": references_validation_results,
                "referencesLookupErrors": references_lookup_errors
            }
        )

    except Exception as error:
        print(f"Error: {str(error)}")

        # Determine appropriate status code and error code based on error type
        status_code = 500
        error_code = "INTERNAL_ERROR"
        error_message = str(error)

        if "Invalid AWS credentials" in error_message:
            status_code = 401
            error_code = "INVALID_CREDENTIALS"
        elif "Failed to upload file to S3" in error_message:
            status_code = 500
            error_code = "S3_UPLOAD_ERROR"
        elif "Failed to process with Bedrock" in error_message:
            status_code = 500
            error_code = "BEDROCK_ERROR"
        elif "Failed to setup S3 bucket" in error_message:
            status_code = 500
            error_code = "S3_SETUP_ERROR"

        return create_response(
            status_code,
            {"success": False, "error": error_message, "errorCode": error_code},
        )


def validate_request(body):
    """Validate required parameters in request body"""
    if not body:
        return "Missing request body"

    missing_params = []

    if not body.get("credentials"):
        missing_params.append("credentials")
    else:
        if not body["credentials"].get("accessKeyId"):
            missing_params.append("credentials.accessKeyId")
        if not body["credentials"].get("secretAccessKey"):
            missing_params.append("credentials.secretAccessKey")

    if not body.get("fileBase64"):
        missing_params.append("fileBase64")
    elif len(base64.b64decode(body["fileBase64"])) > 5 * 1024 * 1024:
        # Check file size (5MB limit)
        return "File size exceeds maximum limit of 5MB"

    if not body.get("prompt"):
        missing_params.append("prompt")

    if not body.get("fileName"):
        missing_params.append("fileName")

    if missing_params:
        return f"Missing required parameters: {', '.join(missing_params)}"

    return None


def validate_credentials(credentials):
    """Validate AWS credentials using STS"""
    try:
        # Create STS client with user-provided credentials
        sts_client = boto3.client(
            "sts",
            region_name=AWS_REGION,
            aws_access_key_id=credentials["accessKeyId"],
            aws_secret_access_key=credentials["secretAccessKey"],
            aws_session_token=credentials.get("sessionToken"),
        )

        # Attempt to get caller identity to validate credentials
        sts_client.get_caller_identity()

        return True
    except Exception as error:
        print(f"Credential validation failed: {str(error)}")
        return False


def ensure_bucket_exists(credentials):
    """Ensure S3 bucket exists with lifecycle policy"""
    try:
        # Create S3 client with user-provided credentials
        s3_client = boto3.client(
            "s3",
            region_name=AWS_REGION,
            aws_access_key_id=credentials["accessKeyId"],
            aws_secret_access_key=credentials["secretAccessKey"],
            aws_session_token=credentials.get("sessionToken"),
        )

        # Check if bucket exists
        try:
            s3_client.head_bucket(Bucket=S3_BUCKET_NAME)
            print(f"Bucket already exists: {S3_BUCKET_NAME}")
        except ClientError as error:
            error_code = error.response.get("Error", {}).get("Code")
            if error_code == "404" or error_code == "NotFound":
                # Bucket doesn't exist, create it
                print(f"Creating bucket: {S3_BUCKET_NAME}")

                s3_client.create_bucket(Bucket=S3_BUCKET_NAME)

                # Set lifecycle policy to expire objects after 30 days
                lifecycle_config = {
                    "Rules": [
                        {
                            "Id": "ExpireUploadsAfter30Days",
                            "Status": "Enabled",
                            "Expiration": {"Days": 30},
                            "Filter": {"Prefix": "uploads/"},
                        },
                        {
                            "Id": "ExpireUploadsAfter30Days",
                            "Status": "Enabled",
                            "Expiration": {"Days": 30},
                            "Filter": {"Prefix": "markdowns/"},
                        },
                    ]
                }

                s3_client.put_bucket_lifecycle_configuration(
                    Bucket=S3_BUCKET_NAME, LifecycleConfiguration=lifecycle_config
                )

                print("Bucket created with lifecycle policy")
            else:
                raise error
    except Exception as error:
        print(f"Error ensuring bucket exists: {str(error)}")
        raise Exception(f"Failed to setup S3 bucket: {str(error)}")


def upload_file_to_s3(credentials, file_content, file_name):
    """Upload file to S3 with potentially non-unique key"""
    try:
        # Create S3 client with user-provided credentials
        s3_client = boto3.client(
            "s3",
            region_name=AWS_REGION,
            aws_access_key_id=credentials["accessKeyId"],
            aws_secret_access_key=credentials["secretAccessKey"],
            aws_session_token=credentials.get("sessionToken"),
        )

        # Generate unique S3 key with timestamp
        timestamp = int(time.time() * 1000)

        sanitized_file_name = _sanitize_file_name(file_name)

        s3_key = f"uploads/{sanitized_file_name}"
        markdown_file_name = f"{sanitized_file_name[: sanitized_file_name.find('.')]}.md"  # strip off .pdf and add .md
        markdown_s3_key = f"markdowns/{markdown_file_name}"

        # Check if file already exists (then md file also exists)
        if _check_file_exists(S3_BUCKET_NAME, s3_key, s3_client) and _check_file_exists(
            S3_BUCKET_NAME, markdown_s3_key, s3_client
        ):
            return {"s3_key": s3_key, "markdown_s3_key": markdown_s3_key}
        else:
            # Upload file to S3
            # Assumes ok with new vewsion / overwrite
            s3_client.put_object(
                Bucket=S3_BUCKET_NAME,
                Key=s3_key,
                Body=file_content,
                ContentType="application/pdf",  # specify pdf
                Metadata={
                    "original-filename": file_name,
                    "upload-timestamp": str(timestamp),
                },
            )

            print(f"File uploaded successfully: {s3_key}")

            # Create markdown file for llm consumption
            print("Trying pymupdf4llm")
            decoded_file_doc = mde.pymupdf.open(
                stream=BytesIO(file_content), filetype="pdf"
            )
            print(f"Got decoded file doc")
            print(f"File doc: \n\n{decoded_file_doc}\n\n")

            markdown_text: str = mde.to_markdown(decoded_file_doc)
            print("Got markdown text")

            # Upload markdown file to S3
            # Assumes ok with new version / overwrite
            s3_client.put_object(
                Bucket=S3_BUCKET_NAME,
                Key=markdown_s3_key,
                Body=markdown_text.encode("utf-8"),
                ContentType="text/markdown; charset=utf8",  # specify markdown
                Metadata={
                    "original-filename": markdown_file_name,
                    "upload-timestamp": str(timestamp),
                },
            )
            print(f"Markdown file uploaded successfully: {markdown_s3_key}")

            return {"s3_key": s3_key, "markdown_s3_key": markdown_s3_key}
    except Exception as error:
        print(f"Error uploading file to S3: {str(error)}")
        raise Exception(f"Failed to upload file to S3: {str(error)}")


def get_markdown_file_from_s3(credentials, markdown_s3_key):
    """Retrieve file content from S3"""
    try:
        # Create S3 client with user-provided credentials
        s3_client = boto3.client(
            "s3",
            region_name=AWS_REGION,
            aws_access_key_id=credentials["accessKeyId"],
            aws_secret_access_key=credentials["secretAccessKey"],
            aws_session_token=credentials.get("sessionToken"),
        )

        # Get file from S3
        # Get md related key
        response = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=markdown_s3_key)

        # Read file content
        file_content = response["Body"].read().decode("utf-8")

        return file_content
    except Exception as error:
        print(f"Error retrieving file from S3: {str(error)}")
        raise Exception(f"Failed to retrieve file from S3: {str(error)}")


def process_with_bedrock(credentials, prompt, file_content):
    """Process text with Amazon Bedrock Claude model"""
    try:

        bedrock_client = boto3.client("bedrock-runtime", region_name=AWS_REGION)

        # Call message construction function
        print("Building prompt")
        messages = _build_prompt(prompt, file_content)

        # Construct the request body for Claude
        request_body = {
            "anthropic_version": "bedrock-2023-05-31",
            "system": SYSTEM_PROMPT,
            "max_tokens": 4096,
            "messages": messages,
        }

        print("Request body built, invoking model")
        # Invoke Bedrock model
        response = bedrock_client.invoke_model(
            modelId=BEDROCK_MODEL_ID,
            contentType="application/json",
            accept="application/json",
            body=json.dumps(request_body),
        )

        # Parse response
        response_body = json.loads(response["body"].read())

        # Extract text from response
        # Potentially expand for tool call result
        if response_body.get("content") and len(response_body["content"]) > 0:
            return response_body["content"][0]["text"]
        else:
            raise Exception("No content in Bedrock response")
    except Exception as error:
        print(f"Error processing with Bedrock: {str(error)}")
        raise Exception(f"Failed to process with Bedrock: {str(error)}")


# Main Bedrock caller using converse API
def process_with_bedrock_converse(credentials, prompt, file_content):
    def _build_converse_messages(prompt: str, file_content: str) -> list[dict]:
        user_prompt = (
            prompt + f"\n\nThe academic paper is as follows:\n\n```{file_content}```"
        )
        return [
            {
                "role": "user",
                # Converse API content blocks use {"text": "..."} (no "type":"text")
                "content": [{"text": user_prompt}],
            }
        ]

    bedrock_client = boto3.client(
        "bedrock-runtime",
        region_name=AWS_REGION,
        aws_access_key_id=credentials["accessKeyId"],
        aws_secret_access_key=credentials["secretAccessKey"],
        aws_session_token=credentials.get("sessionToken"),
    )

    messages = _build_converse_messages(prompt, file_content)

    system_blocks = [{"text": SYSTEM_PROMPT}]

    inference_cfg = {
        "maxTokens": 4096,
        # Optional:
        # "temperature": 0.3,
        # "topP": 0.9,
        # "stopSequences": ["</end>"],
    }

    print(f"Calling converse model {BEDROCK_MODEL_ID}")

    resp = bedrock_client.converse(
        modelId=BEDROCK_MODEL_ID,
        system=system_blocks,
        messages=messages,
        inferenceConfig=inference_cfg,
    )

    # Text output
    text = resp["output"]["message"]["content"][0]["text"]

    if len(text) > 0:
        return text
    else:
        raise Exception("Bedrock Nova call returned text length of 0")


def create_response(status_code, body):
    """Create HTTP response - CORS handled by Function URL"""
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def _sanitize_file_name(file_name: str) -> str:
    sanitized_file_name: str = "".join(
        c if c.isalnum() or c in ".-" else "_" for c in file_name
    )
    return sanitized_file_name


def _build_prompt(prompt: str, file_content: str) -> list[dict[str, str]]:
    # Construct user prompt

    user_prompt = (
        prompt + f"\n\nThe academic paper is as follows:\n\n```{file_content}```"
    )
    messages = [
        {
            "role": "user",
            "content": [{"type": "text", "text": user_prompt}],
        }
    ]

    return messages


def _check_file_exists(bucket: str, key: str, s3_client) -> bool:
    try:
        s3_client.head_object(Bucket=bucket, Key=key)
        print(f"File {key} already exits in s3")
        return True
    except ClientError as e:
        return False


# CODE FOR REFERENCES EXTRACTION 

# To be placed in the main lambda handler
def extract_and_upload_references(
    markdown_text: str, file_name: str, ref_title: str, credentials: Dict[str, str]
) -> None:
    """
    Extract the references section from the Markdown.
    Uploads the file to S3.
    """

    # Check if references exist
    # If do not exist extract and upload

    s3_client = boto3.client(
        "s3",
        region_name=AWS_REGION,
        aws_access_key_id=credentials["accessKeyId"],
        aws_secret_access_key=credentials["secretAccessKey"],
        aws_session_token=credentials.get("sessionToken"),
    )

    sanitized_file_name = _sanitize_file_name(file_name)
    references_file_name = f"{sanitized_file_name[: sanitized_file_name.find('.')]}.json"  # strip off .pdf and add .json
    s3_key_references = f"references_json/{references_file_name}"

    if _check_file_exists(S3_BUCKET_NAME, s3_key_references, s3_client):
        print(f"A references extraction {s3_key_references} already exists.")
    else:
        references_json_data = run_references_extraction(
            markdown_text, ref_title, credentials
        )
        if references_json_data is None:
            print(
                f"Could not find text in {file_name} with references section {ref_title}."
            )
            return

        timestamp = int(time.time() * 1000)

        # Upload references json extraction to S3
        s3_client.put_object(
            Bucket=S3_BUCKET_NAME,
            Key=s3_key_references,
            Body=references_json_data,
            ContentType="application/json",
            Metadata={
                "original-filename": file_name,
                "upload-timestamp": str(timestamp),
            },
        )

        print(
            f"References json file extraction uploaded successfully: {s3_key_references}"
        )


def run_references_extraction(
    markdown_text: str, ref_title: str, credentials: Dict[str, str]
):  # Returns json string of data or None if there nothing found
    # Check if the length (hardcoded at 6000) and process accordingly
    extracted_references_indices = _find_section_span_simple(markdown_text, ref_title)
    if extracted_references_indices is None:
        print(f"Cannot find {ref_title} section in markdown text")
        return

    if extracted_references_indices[1] is None:
        start_index = extracted_references_indices[0]
        end_index = _find_next_marked_heading_char_v2(
            markdown_text, extracted_references_indices[0]
        )

    extracted_references = markdown_text[start_index:end_index]

    if len(extracted_references) > 6000:
        text_chunks = _split_markdown_text(extracted_references)
        split_results = []

        for x in enumerate(text_chunks):
            print(f"Calling split {x[0]} on extracted references")
            res = _process_with_bedrock_converse_bib_tool(
                credentials, x[1], SYSTEM_PROMPT_FOR_REFERENCES_EXCTRACTION, TOOL_LIST
            )
            split_results.append(res)

        results_dicts = [
            x[0]["toolUse"]["input"]["references_list"] for x in split_results
        ]  # list of lists of dicts
        final_list = list(itertools.chain.from_iterable(results_dicts))

        return json.dumps(final_list)

    else:
        res = _process_with_bedrock_converse_bib_tool(
            credentials,
            extracted_references,
            SYSTEM_PROMPT_FOR_REFERENCES_EXCTRACTION,
            TOOL_LIST,
        )

        return json.dumps(res[0]["toolUse"]["input"]["references_list"])


# Simple heuristic method of finding the start of References/Citations/etc. section
def _find_section_span_simple(
    md: str, ref_title: str, max_line_len: int = 30
) -> Optional[Tuple[int, Optional[int]]]:
    """
    Start:
      - Find a short line (< max_line_len chars stripped) that is basically:
        optional ### + optional **** + ref_title + optional **** + optional ':' or '.'

    End:
      - The next line that starts with '#' (ATX heading), or EOF.
    Returns (start_char, end_char_or_None).
    """

    lines = md.splitlines(keepends=True)

    # Char offsets for line starts
    offsets = [0]
    for ln in lines:
        offsets.append(offsets[-1] + len(ln))

    # Very simple start pattern
    start_re = re.compile(
        rf"^\s*#*\s*[*_]*\s*{re.escape(ref_title.strip())}\s*[*_]*\s*[:.]?\s*$",
        re.IGNORECASE,
    )

    # Simplest "next heading" detector
    end_re = re.compile(r"^\s*#{1,6}\s+\S")

    start_line = None

    # Find start
    for i, raw in enumerate(lines):
        line = raw.rstrip("\r\n")
        if len(line.strip()) >= max_line_len:
            continue
        if start_re.match(line):
            start_line = i
            break

    if start_line is None:
        return None

    start_char = offsets[start_line]

    # Find end
    for j in range(start_line + 1, len(lines)):
        if end_re.match(lines[j]):
            return start_char, offsets[j]

    return start_char, None


# Simple heuristic method of finding the start of section following the References/Citations/etc. section
def _find_next_marked_heading_char_v2(md: str, start_char: int) -> Optional[int]:
    # Corrects for case where paper has header title across top of each page which
    # may cause an accidental "too early" regex match on a new section. Note false negative case simply
    # means that another section is incorporated but hopefully LLM tool call prompt successfully guides this not to be processed in
    # structured json extraction
    # Group 1: The artifact (page number) - Optional
    # Group 2: The actual heading - Required
    COMBINED_PATTERN = re.compile(
        r"""
        (?:(?P<artifact>\n+[ \t]*\d+[ \t]*\n+))? # Optional Page Number block
        ^[ \t]*
        (?P<heading>                             # The actual heading
            \#{1,}[ \t]+\S[^\n]*
            |
            \*{1,}[ \t]*\S[^\n]*
        )[ \t]*$
        """,
        re.VERBOSE | re.MULTILINE,
    )

    nl = md.find("\n", start_char)
    pos = (nl + 1) if nl != -1 else len(md)

    while True:
        m = COMBINED_PATTERN.search(md, pos=pos)
        if not m:
            return None

        # Logic: If the 'artifact' group matched, it means this heading
        # is immediately preceded by a page number. We skip it.
        if m.group("artifact"):
            pos = m.end()
            continue

        # Otherwise, it's a clean heading match.
        # We return the start of the heading group specifically.
        return m.start("heading")


# For splitting large entities.
def _split_markdown_text(
    text: str,
    *,
    threshold: int = 6000,
    max_len: int = 4000,
    overlap_lines_if_no_double_newline: int = 5,
    normalize_newlines: bool = True,
) -> List[str]:
    """
    Read a markdown file and split it into chunks.

    Behavior:
    - If file length <= threshold: returns [full_text]
    - Else: splits into chunks with max length max_len, choosing split points:
        1) Prefer the closest split <= max_len that ends with "\\n\\n"
        2) If none, fallback to the closest split <= max_len that ends with "\\n"
        3) If none, hard-split at max_len
    - If the split was NOT done on "\\n\\n", the next chunk will include an overlap
      of `overlap_lines_if_no_double_newline` lines from the end of the previous chunk
      (when possible).

    Returns:
      List[str] of chunks.
    """
    if threshold <= max_len:
        raise ValueError(
            "threshold must be greater than max_len to match the requested behavior."
        )

    if normalize_newlines:
        text = text.replace("\r\n", "\n").replace("\r", "\n")

    if len(text) <= threshold:
        return [text]

    def _overlap_start_index(chunk_text: str, want_lines: int) -> int:
        """
        Return the start index (within chunk_text) of the last `want_lines` lines.
        If chunk has fewer/equal lines, return 0.
        """
        if want_lines <= 0:
            return len(chunk_text)

        # Build line start positions: [0, idx_after_1st_newline, idx_after_2nd_newline, ...]
        starts = [0]
        for pos, ch in enumerate(chunk_text):
            if ch == "\n":
                starts.append(pos + 1)

        if len(starts) <= want_lines:
            return 0
        return starts[-want_lines]

    chunks: List[str] = []
    i = 0
    n = len(text)

    while i < n:
        remaining = n - i
        if remaining <= max_len:
            chunks.append(text[i:])
            break

        window = text[i : i + max_len]

        dd = window.rfind("\n\n")
        if dd != -1:
            split_at = i + dd + 2
            split_kind = "double"
        else:
            sn = window.rfind("\n")
            if sn != -1:
                split_at = i + sn + 1
                split_kind = "single"
            else:
                split_at = i + max_len
                split_kind = "hard"

        # Safety: ensure forward progress
        if split_at <= i:
            split_at = i + max_len
            split_kind = "hard"

        chunk = text[i:split_at]
        chunks.append(chunk)

        # Default next start is just after the split.
        next_i = split_at

        # If we couldn't split on double newline, add line overlap (if possible).
        if split_kind != "double" and overlap_lines_if_no_double_newline > 0:
            back = _overlap_start_index(chunk, overlap_lines_if_no_double_newline)
            overlap_i = i + back

            # Only apply overlap if it still makes progress (avoid infinite loops).
            if overlap_i > i:
                next_i = overlap_i

        i = next_i

    return chunks


# Main Bedrock caller using converse API
def _process_with_bedrock_converse_bib_tool(
    credentials, text_content, system_prompt_references, tool_list
):
    def _build_converse_messages(text_content: str) -> list[dict]:
        user_prompt = f"Please parse these references: \n<text_to_parse>{text_content}\n</text_to_parse>"

        return [
            {
                "role": "user",
                # Converse API content blocks use {"text": "..."} (no "type":"text")
                "content": [{"text": user_prompt}],
            }
        ]

    bedrock_client = boto3.client(
        "bedrock-runtime",
        region_name=AWS_REGION,
        aws_access_key_id=credentials["accessKeyId"],
        aws_secret_access_key=credentials["secretAccessKey"],
        aws_session_token=credentials.get("sessionToken"),
    )

    messages = _build_converse_messages(text_content)

    system_blocks = [{"text": system_prompt_references}]

    inference_cfg = {
        # "maxTokens": 4096,
        # Optional:
        "temperature": 0.0,
        "topP": 1.0,
        # "stopSequences": ["</end>"],
    }

    print(f"Calling converse model {BEDROCK_MODEL_ID} for references extraction")

    resp = bedrock_client.converse(
        modelId=BEDROCK_MODEL_ID,
        system=system_blocks,
        messages=messages,
        inferenceConfig=inference_cfg,
        toolConfig={
            "tools": tool_list,
            "toolChoice": {"tool": {"name": "submit_bibliography"}},  # FORCED MODE
            # additionalModelRequestFields=additional,
        },
    )

    # Text output
    text = resp["output"]["message"]["content"]  # [0]["text"]

    if len(text) > 0:
        return text
    else:
        raise Exception("Bedrock Nova call returned text length of 0")





# REFERENCE VALIDATION FUNCTIONALITY


def validate_references(
    file_name: str,
    references_title: str,
    credentials: Dict[str, str]
) -> Tuple[List[Tuple[Dict, Dict]], List[str]]:
    """
    Validate extracted references against Semantic Scholar using LLM comparison.

    Returns:
        Tuple of:
        - List of (cur_ref, sretrieved) tuples for mismatched references
        - List of reference titles that failed due to API errors (rate limiting, etc.)
    """
    # 1. Get sanitized filename and build S3 key
    sanitized_file_name = _sanitize_file_name(file_name)
    json_file_name = f"{sanitized_file_name[:sanitized_file_name.find('.')]}.json"
    s3_key = f"references_json/{json_file_name}"

    # 2. Create S3 client and retrieve references JSON
    s3_client = boto3.client(
        "s3",
        region_name=AWS_REGION,
        aws_access_key_id=credentials["accessKeyId"],
        aws_secret_access_key=credentials["secretAccessKey"],
        aws_session_token=credentials.get("sessionToken"),
    )

    try:
        response = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=s3_key)
        references = json.loads(response["Body"].read().decode("utf-8"))
    except Exception as e:
        print(f"Error retrieving references JSON: {e}")
        return [], []

    # 3. Create Bedrock client
    bedrock_client = boto3.client(
        "bedrock-runtime",
        region_name=AWS_REGION,
        aws_access_key_id=credentials["accessKeyId"],
        aws_secret_access_key=credentials["secretAccessKey"],
        aws_session_token=credentials.get("sessionToken"),
    )

    references_problem_return_list: List[Tuple[Dict, Dict]] = []
    error_requests: List[str] = []

    # 4. Loop through references and validate each
    for ref in references:
        ref_title = ref.get("title", "unknown")

        try:
            # a) Query Semantic Scholar
            data_list, error_code = semantic_scholar_lookup(ref_title)

            # Handle API errors
            if error_code is not None:
                print(f"Semantic Scholar error {error_code} for: {ref_title[:50]}...")
                error_requests.append(ref_title)
                continue

            # Handle empty results
            if not data_list:
                print(f"No Semantic Scholar results for: {ref_title[:50]}...")
                error_requests.append(ref_title)
                continue

            # Get first result
            ro = data_list[0]

            # b) Build comparison dictionaries
            sretrieved = {
                "title": ro.get("title", ""),
                "authors": ", ".join([a.get("name", "") for a in ro.get("authors", [])]),
                "year": ro.get("year")
            }

            cur_ref = {
                "title": ref.get("title", ""),
                "authors": ref.get("authors", ""),
                "year": ref.get("year")
            }

            # c) Call Bedrock for comparison
            user_prompt = f"""Compare the following two dictionaries and their fields and analyze if they represent a reference to the same paper. If you assess that the papers are the same based on the values for the title, authors, and date keys, you will output the word SAME (use all capitals). If you perceive that the discrepancy between the two sets of data is too large, then output the word DIFFERENT (use all capitals).

The papers you are to compare are summarized by the data in:
{cur_ref}

And the data in:
{sretrieved}

Remember, You are to **only** output the word SAME or the word DIFFERENT. Do not output any other words."""
            print(f"Calling bedrock for {ref_title}")
            resp = bedrock_client.converse(
                modelId=BEDROCK_MODEL_ID,
                system=[{"text": SYSTEM_PROMPT_FOR_COMPARISON_OF_REFERENCES}],
                messages=[{
                    "role": "user",
                    "content": [{"text": user_prompt}]
                }]
            )

            result_text = resp["output"]["message"]["content"][0]["text"].strip()
            print(f"Comparison result for '{ref_title[:30]}...': {result_text}")

            # d) Check result and add to problem list if DIFFERENT
            if "DIFFERENT" in result_text:
                references_problem_return_list.append((cur_ref, sretrieved))

        except Exception as e:
            print(f"Error validating reference '{ref_title}': {e}")
            error_requests.append(ref_title)
            continue

    return references_problem_return_list, error_requests


def semantic_scholar_lookup(
    title: str,
    max_retries: int = 3,
    base_delay: float = 1.0
) -> Tuple[Optional[List[Dict]], Optional[int]]:
    """
    Query Semantic Scholar API for a paper by title.

    Returns:
        Tuple of (data_list, error_code):
        - On success: (list of paper dicts, None)
        - On rate limit after retries: (None, 429)
        - On other error: (None, status_code)
        - On empty results: ([], None)
    """
    url = "https://api.semanticscholar.org/graph/v1/paper/search"
    params = {
        'query': title,
        'fields': 'title,authors,year'
    }

    for attempt in range(max_retries):
        try:
            response = requests.get(url, params=params, timeout=10)

            if response.status_code == 200:
                data = response.json()
                return data.get('data', []), None

            elif response.status_code == 429:
                # Rate limited - use exponential backoff
                wait_time = int(response.headers.get("Retry-After", base_delay * (2 ** attempt)))
                print(f"Rate limited (attempt {attempt + 1}/{max_retries}). Waiting {wait_time}s...")
                time.sleep(wait_time)
                continue

            else:
                print(f"Semantic Scholar API error: {response.status_code}")
                return None, response.status_code

        except requests.exceptions.Timeout:
            print(f"Request timeout (attempt {attempt + 1}/{max_retries})")
            time.sleep(base_delay * (2 ** attempt))
            continue
        except requests.exceptions.RequestException as e:
            print(f"Request error: {e}")
            return None, -1

    # Exhausted retries (likely rate limited)
    print(f"Exhausted {max_retries} retries for: {title[:50]}...")
    return None, 429


# UNUSED (BUT USABLE) FUNCTIONALITY


def markdown_from_llm_anthropic_model(pdf_b64, filename, max_tokens):
    client = boto3.client("bedrock-runtime")

    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": max_tokens,
        "system": (
            "You convert PDFs into clean, semantically-correct Markdown. "
            "Preserve headings, lists, tables, footnotes, and links. "
            "Keep reading order. Output only Markdown with no extra commentary. "
            "If the PDF has images or charts, insert descriptive placeholders "
            "like: ![figure - page <n>](image-page-<n>.png)."
        ),
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": pdf_b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": "Convert this entire document to Markdown.",
                    },
                ],
            }
        ],
    }

    resp = client.invoke_model(modelId=BEDROCK_MODEL_ID, body=json.dumps(body))
    payload = json.loads(resp["body"].read())

    # Claude's Bedrock Messages response is an array of content blocks; concat text blocks.
    md = "".join(
        block.get("text", "")
        for block in payload.get("content", [])
        if block.get("type") == "text"
    )
    return md