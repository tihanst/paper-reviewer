# Academic Paper Reviewer

An AWS Lambda-based application that analyzes academic papers using AI, extracting references and validating against Semantic Scholar. Upload a PDF, provide a prompt, and receive intelligent analysis powered by AWS Bedrock.

## Features

- **PDF Analysis**: Upload academic papers and get AI-powered reviews and analysis
- **PDF to Markdown Conversion**: Converts PDFs to LLM-optimized markdown using pymupdf4llm
- **Reference Extraction**: Automatically extracts and structures bibliographic references with LLM and forced tool use
- **Reference Lookup**: Validates references against Semantic Scholar
- **S3 Caching**: Previously uploaded files are cached for faster subsequent analysis
- **Multi-Model Support**: Works with AWS Bedrock's Converse API. 

## Architecture

```
┌─────────────┐     ┌─────────────────┐     ┌─────────────┐
│   Frontend  │────▶│  AWS Lambda     │────▶│  S3 Bucket  │
│ (index.html)│     │                 │     │             │
└─────────────┘     │  - PDF upload   │     │ - uploads/  │
                    │  - Markdown     │     │ - markdowns/│
                    │    conversion   │     │ - refs_json/│
                    │  - Ref extract  │     └─────────────┘
                    └────────┬────────┘
                             │
                             ▼
                    ┌─────────────────┐
                    │  AWS Bedrock    │
                    │  (Claude/Nova)  │
                    └─────────────────┘
```

### Components

| Component | Description |
|-----------|-------------|
| `lambda_main.py` | Main Lambda handler with PDF processing and Bedrock integration |
| `index.html` | Web frontend HTML structure |
| `styles.css` | Frontend styling |
| `app.js` | Frontend JavaScript (includes Lambda endpoint configuration) |
| `resources/` | Pre-built Lambda layer with Linux-compatible pymupdf binaries |
| S3 Bucket | Stores PDFs, markdown conversions, and extracted references |

## Prerequisites

- AWS Account with access to:
  - Lambda
  - S3
  - Bedrock (with model access enabled)
  - STS
- Python 3.12
- AWS CLI configured (for deployment)

## Dependencies

```
boto3
botocore
pymupdf4llm
requests
```

## Configuration

Edit the following constants in `lambda_main.py`:

```python
S3_BUCKET_NAME = "Your S3 bucket name"
AWS_REGION = "Your AWS region"
BEDROCK_MODEL_ID = "us.amazon.nova-micro-v1:0"  # Bedrock model ID
```

### Supported Models (invoke_model only for unutilized LLM-based pdf-to-markdown functionality)

| Model Type | API | Model ID Example |
|------------|-----|------------------|
| Amazon Nova | Converse API | `us.amazon.nova-micro-v1:0` |
| Claude | invoke_model API | `us.anthropic.claude-3-5-sonnet-20241022-v2:0` |

## Deployment

### Automated Deployment (Recommended)

Use the included `deploy.sh` script:

```bash
# 1. Edit deploy.sh to set your S3_BUCKET_NAME and AWS_REGION

# 2. Run full deployment (creates IAM role, Lambda layers, function, and URL)
./deploy.sh

# Or use these options for different scenarios:
./deploy.sh --skip-iam         # Skip IAM role creation if you already have one
./deploy.sh --role-arn <arn>   # Use a specific existing IAM role ARN
./deploy.sh --update-code      # Only update Lambda code (faster iteration)
```

**IAM Note:** The script creates an IAM role with necessary permissions by default. If your organization has IAM restrictions or you prefer to manage roles separately, use `--skip-iam` or `--role-arn` and create the role manually with the permissions listed below.

### Manual Deployment

1. **Create Lambda Function**
   - Runtime: Python 3.12
   - Handler: `lambda_main.lambda_handler`
   - Timeout: Recommend 5+ minutes for large papers
   - Memory: Recommend 512MB+

2. **Add Lambda Layers** (two layers required due to size constraints):
   - **Layer 1 (pymupdf)**: Use the pre-built layer in `resources/python312_pymupdf4llm.zip`
   - **Layer 2 (utilities)**: `requests`

   Note: The `resources/python312_pymupdf4llm.zip` contains a slimmed-down, Linux-compatible build of pymupdf 1.26.6, pymupdf4llm 0.2.6, and tabulate. This was pre-built on Linux to ensure compatibility with Lambda's Amazon Linux runtime. Building pymupdf4llm from pip on macOS will not work due to architecture differences.

3. **Configure IAM Permissions**:
   ```json
   {
     "Effect": "Allow",
     "Action": [
       "s3:GetObject",
       "s3:PutObject",
       "s3:CreateBucket",
       "s3:PutLifecycleConfiguration",
       "bedrock:InvokeModel",
       "sts:GetCallerIdentity"
     ],
     "Resource": "*"
   }
   ```

4. **Enable Function URL** with CORS support

5. **Update Frontend**
   - Set the `LAMBDA_ENDPOINT` URL in `app.js`

## Usage

### Web Interface

1. Open `index.html` in a browser
2. Enter your AWS credentials (Access Key ID, Secret Access Key, optional Session Token)
3. Upload a PDF file (max 5MB)
4. Enter your analysis prompt
5. Optionally specify the exact references section title (Could be "References", "Citations", etc. check paper)
6. Click "Analyze Paper"

### API Request Format

```json
{
  "body": {
    "credentials": {
      "accessKeyId": "AKIA...",
      "secretAccessKey": "...",
      "sessionToken": "..."
    },
    "fileBase64": "<base64-encoded-pdf>",
    "prompt": "Summarize the key findings of this paper",
    "fileName": "paper.pdf",
    "referencesTitle": "Whatever the section is called"
  }
}
```

### API Response Format

**Success (200):**
```json
{
  "statusCode": 200,
  "body": {
    "success": true,
    "result": "<AI analysis result>",
    "s3Key": "<S3 key for the markdown file>",
    "referencesValidation": [
      {
        "reference": "<original reference>",
        "mismatch": "<description of mismatch>"
      }
    ],
    "referencesLookupErrors": [
      "<references that could not be found>"
    ]
  }
}
```

**Error:**
```json
{
  "statusCode": 401|500,
  "body": {
    "success": false,
    "error": "<error message>",
    "errorCode": "INVALID_CREDENTIALS|S3_UPLOAD_ERROR|BEDROCK_ERROR|INTERNAL_ERROR"
  }
}
```

## How It Works

1. **Credential Validation**: Verifies AWS credentials using STS
2. **Bucket Setup**: Creates S3 bucket with 30-day lifecycle if it doesn't exist
3. **File Upload**: Uploads PDF to `uploads/` prefix (or reuses cached version)
4. **Markdown Conversion**: Converts PDF to markdown optimized for LLM consumption
5. **AI Analysis**: Sends markdown + prompt to Bedrock for analysis
6. **Reference Extraction**: Extracts bibliographic references using forced tool use
7. **Reference Validation**: Looks up references on Semantic Scholar

## File Size Limits

- Maximum PDF size: **5MB**

## S3 Bucket Structure

```
bucket/
├── uploads/          # Original PDF files
├── markdowns/        # Converted markdown files
└── references_json/  # Extracted reference data
```

Files automatically expire after 30 days via lifecycle policy.

## License

MIT
