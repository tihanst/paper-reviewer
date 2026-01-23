#!/bin/bash
set -e

# ============================================================================
# Academic Paper Reviewer - AWS Lambda Deployment Script
# ============================================================================
#
# Prerequisites:
#   - AWS CLI installed and configured (aws configure)
#   - zip utility installed
#   - Pre-built Lambda layer: resources/python312_pymupdf4llm.zip
#
# Pre-built Layer:
#   The resources/python312_pymupdf4llm.zip contains Linux-compatible binaries:
#     - pymupdf 1.26.6 (with native .so files for Amazon Linux)
#     - pymupdf4llm 0.2.6
#     - tabulate 0.9.0
#   This layer was pre-built on Linux because pymupdf's native binaries
#   cannot be cross-compiled from macOS.
#
# Usage:
#   ./deploy.sh                           # Full deployment with IAM setup
#   ./deploy.sh --skip-iam                # Skip IAM role creation (use existing role)
#   ./deploy.sh --role-arn <arn>          # Use specific existing IAM role
#   ./deploy.sh --update-code             # Only update Lambda code (skip layer/IAM)
#
# ============================================================================

# Configuration - EDIT THESE VALUES
S3_BUCKET_NAME=""           # Your S3 bucket name for storing papers
AWS_REGION=""               # AWS region (e.g., us-east-1)
LAMBDA_FUNCTION_NAME="paper-reviewer"
LAMBDA_LAYER_NAME="paper-reviewer-deps"
IAM_ROLE_NAME="paper-reviewer-lambda-role"

# Derived values
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="${SCRIPT_DIR}/.build"
LAYER_DIR="${BUILD_DIR}/layer"
LAMBDA_ZIP="${BUILD_DIR}/lambda.zip"
LAYER_ZIP="${BUILD_DIR}/layer.zip"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# ============================================================================
# Helper Functions
# ============================================================================

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

check_prerequisites() {
    log_info "Checking prerequisites..."

    if ! command -v aws &> /dev/null; then
        log_error "AWS CLI not found. Install it: https://aws.amazon.com/cli/"
        exit 1
    fi

    if ! command -v python3 &> /dev/null; then
        log_error "Python 3 not found."
        exit 1
    fi

    if ! command -v pip3 &> /dev/null && ! command -v pip &> /dev/null; then
        log_error "pip not found."
        exit 1
    fi

    if ! command -v zip &> /dev/null; then
        log_error "zip not found."
        exit 1
    fi

    # Check AWS credentials
    if ! aws sts get-caller-identity &> /dev/null; then
        log_error "AWS credentials not configured. Run 'aws configure'"
        exit 1
    fi

    log_info "All prerequisites met."
}

validate_config() {
    if [[ -z "$S3_BUCKET_NAME" ]]; then
        log_error "S3_BUCKET_NAME not set. Edit this script and set the configuration values."
        exit 1
    fi

    if [[ -z "$AWS_REGION" ]]; then
        log_error "AWS_REGION not set. Edit this script and set the configuration values."
        exit 1
    fi

    log_info "Configuration validated."
}

# ============================================================================
# IAM Role Setup
# ============================================================================

create_iam_role() {
    log_info "Creating IAM role: ${IAM_ROLE_NAME}..."

    # Check if role already exists
    if aws iam get-role --role-name "$IAM_ROLE_NAME" &> /dev/null; then
        log_warn "IAM role ${IAM_ROLE_NAME} already exists. Using existing role."
        ROLE_ARN=$(aws iam get-role --role-name "$IAM_ROLE_NAME" --query 'Role.Arn' --output text)
        return 0
    fi

    # Trust policy for Lambda
    TRUST_POLICY='{
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {
                    "Service": "lambda.amazonaws.com"
                },
                "Action": "sts:AssumeRole"
            }
        ]
    }'

    # Create the role
    ROLE_ARN=$(aws iam create-role \
        --role-name "$IAM_ROLE_NAME" \
        --assume-role-policy-document "$TRUST_POLICY" \
        --description "Execution role for Paper Reviewer Lambda" \
        --query 'Role.Arn' \
        --output text)

    log_info "Created IAM role: ${ROLE_ARN}"

    # Attach basic Lambda execution policy (for CloudWatch Logs)
    aws iam attach-role-policy \
        --role-name "$IAM_ROLE_NAME" \
        --policy-arn "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"

    # Create and attach custom policy for S3, Bedrock, STS
    CUSTOM_POLICY='{
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "S3Access",
                "Effect": "Allow",
                "Action": [
                    "s3:GetObject",
                    "s3:PutObject",
                    "s3:DeleteObject",
                    "s3:ListBucket"
                ],
                "Resource": [
                    "arn:aws:s3:::'"${S3_BUCKET_NAME}"'",
                    "arn:aws:s3:::'"${S3_BUCKET_NAME}"'/*"
                ]
            },
            {
                "Sid": "S3BucketManagement",
                "Effect": "Allow",
                "Action": [
                    "s3:CreateBucket",
                    "s3:PutLifecycleConfiguration"
                ],
                "Resource": "arn:aws:s3:::'"${S3_BUCKET_NAME}"'"
            },
            {
                "Sid": "BedrockAccess",
                "Effect": "Allow",
                "Action": [
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream"
                ],
                "Resource": "*"
            },
            {
                "Sid": "STSAccess",
                "Effect": "Allow",
                "Action": "sts:GetCallerIdentity",
                "Resource": "*"
            }
        ]
    }'

    POLICY_NAME="${IAM_ROLE_NAME}-policy"

    # Check if policy exists and delete it first (for updates)
    if aws iam get-role-policy --role-name "$IAM_ROLE_NAME" --policy-name "$POLICY_NAME" &> /dev/null; then
        aws iam delete-role-policy --role-name "$IAM_ROLE_NAME" --policy-name "$POLICY_NAME"
    fi

    aws iam put-role-policy \
        --role-name "$IAM_ROLE_NAME" \
        --policy-name "$POLICY_NAME" \
        --policy-document "$CUSTOM_POLICY"

    log_info "Attached policies to IAM role."

    # Wait for role to propagate
    log_info "Waiting for IAM role to propagate (10 seconds)..."
    sleep 10
}

# ============================================================================
# Lambda Layer Build
# ============================================================================

build_lambda_layer() {
    log_info "Using pre-built Lambda layer for pymupdf4llm..."

    # -------------------------------------------------------------------------
    # Pre-built pymupdf4llm layer
    # -------------------------------------------------------------------------
    # The layer zip contains Linux-compatible binaries for:
    #   - pymupdf 1.26.6 (with native .so files for Amazon Linux)
    #   - pymupdf4llm 0.2.6
    #   - tabulate 0.9.0
    #   - fitz (legacy interface)
    #
    # This was pre-built on a Linux environment to ensure compatibility.
    # -------------------------------------------------------------------------

    PREBUILT_LAYER="${SCRIPT_DIR}/resources/python312_pymupdf4llm.zip"

    if [[ ! -f "$PREBUILT_LAYER" ]]; then
        log_error "Pre-built layer not found at: ${PREBUILT_LAYER}"
        log_error "Please ensure resources/python312_pymupdf4llm.zip exists."
        exit 1
    fi

    # Use the pre-built layer directly
    mkdir -p "${BUILD_DIR}"
    cp "$PREBUILT_LAYER" "${LAYER_ZIP}"

    LAYER_SIZE=$(du -h "${LAYER_ZIP}" | cut -f1)
    log_info "Using pre-built layer: ${LAYER_ZIP} (${LAYER_SIZE})"
}

build_utilities_layer() {
    log_info "Building utilities layer (requests)..."

    UTILS_LAYER_DIR="${BUILD_DIR}/utils_layer"
    rm -rf "${UTILS_LAYER_DIR}"
    mkdir -p "${UTILS_LAYER_DIR}/python/lib/python3.12/site-packages"

    SITE_PACKAGES="${UTILS_LAYER_DIR}/python/lib/python3.12/site-packages"

    pip3 install \
        --target "$SITE_PACKAGES" \
        --upgrade \
        requests \
        2>&1 | grep -v "already satisfied" || true

    # Create layer zip
    UTILS_LAYER_ZIP="${BUILD_DIR}/utils_layer.zip"
    cd "${UTILS_LAYER_DIR}"
    zip -r "${UTILS_LAYER_ZIP}" python -x "*.pyc" -x "*__pycache__*" > /dev/null
    cd "${SCRIPT_DIR}"

    UTILS_LAYER_SIZE=$(du -h "${UTILS_LAYER_ZIP}" | cut -f1)
    log_info "Utilities layer zip created: ${UTILS_LAYER_ZIP} (${UTILS_LAYER_SIZE})"
}

deploy_lambda_layer() {
    log_info "Deploying pymupdf Lambda layer..."

    # Publish pymupdf layer version
    LAYER_VERSION_ARN=$(aws lambda publish-layer-version \
        --layer-name "$LAMBDA_LAYER_NAME" \
        --description "PyMuPDF dependencies for Paper Reviewer Lambda" \
        --zip-file "fileb://${LAYER_ZIP}" \
        --compatible-runtimes python3.12 \
        --region "$AWS_REGION" \
        --query 'LayerVersionArn' \
        --output text)

    log_info "PyMuPDF layer deployed: ${LAYER_VERSION_ARN}"
}

deploy_utilities_layer() {
    log_info "Deploying utilities Lambda layer..."

    UTILS_LAYER_ZIP="${BUILD_DIR}/utils_layer.zip"
    UTILS_LAYER_NAME="${LAMBDA_LAYER_NAME}-utils"

    # Publish utilities layer version
    UTILS_LAYER_VERSION_ARN=$(aws lambda publish-layer-version \
        --layer-name "$UTILS_LAYER_NAME" \
        --description "Utilities (requests) for Paper Reviewer Lambda" \
        --zip-file "fileb://${UTILS_LAYER_ZIP}" \
        --compatible-runtimes python3.12 \
        --region "$AWS_REGION" \
        --query 'LayerVersionArn' \
        --output text)

    log_info "Utilities layer deployed: ${UTILS_LAYER_VERSION_ARN}"
}

# ============================================================================
# Lambda Function Deployment
# ============================================================================

update_lambda_config() {
    log_info "Updating lambda_main.py with configuration values..."

    # Create a temporary copy with updated config
    TEMP_LAMBDA="${BUILD_DIR}/lambda_main.py"
    cp "${SCRIPT_DIR}/lambda_main.py" "$TEMP_LAMBDA"

    # Update configuration values using sed
    if [[ "$OSTYPE" == "darwin"* ]]; then
        # macOS sed requires empty string for -i
        sed -i '' "s/^S3_BUCKET_NAME = .*/S3_BUCKET_NAME = \"${S3_BUCKET_NAME}\"/" "$TEMP_LAMBDA"
        sed -i '' "s/^AWS_REGION = .*/AWS_REGION = \"${AWS_REGION}\"/" "$TEMP_LAMBDA"
    else
        # Linux sed
        sed -i "s/^S3_BUCKET_NAME = .*/S3_BUCKET_NAME = \"${S3_BUCKET_NAME}\"/" "$TEMP_LAMBDA"
        sed -i "s/^AWS_REGION = .*/AWS_REGION = \"${AWS_REGION}\"/" "$TEMP_LAMBDA"
    fi

    log_info "Configuration values updated in Lambda code."
}

package_lambda() {
    log_info "Packaging Lambda function..."

    mkdir -p "${BUILD_DIR}"
    update_lambda_config

    # Create Lambda zip from the updated temp file
    cd "${BUILD_DIR}"
    zip -j "${LAMBDA_ZIP}" "lambda_main.py" > /dev/null
    cd "${SCRIPT_DIR}"

    LAMBDA_SIZE=$(du -h "${LAMBDA_ZIP}" | cut -f1)
    log_info "Lambda zip created: ${LAMBDA_ZIP} (${LAMBDA_SIZE})"
}

deploy_lambda_function() {
    log_info "Deploying Lambda function..."

    # Check if function exists
    if aws lambda get-function --function-name "$LAMBDA_FUNCTION_NAME" --region "$AWS_REGION" &> /dev/null; then
        log_info "Updating existing Lambda function..."

        # Update code
        aws lambda update-function-code \
            --function-name "$LAMBDA_FUNCTION_NAME" \
            --zip-file "fileb://${LAMBDA_ZIP}" \
            --region "$AWS_REGION" \
            --query 'FunctionArn' \
            --output text > /dev/null

        # Wait for update to complete
        aws lambda wait function-updated --function-name "$LAMBDA_FUNCTION_NAME" --region "$AWS_REGION"

        # Update configuration (attach both layers)
        aws lambda update-function-configuration \
            --function-name "$LAMBDA_FUNCTION_NAME" \
            --layers "$LAYER_VERSION_ARN" "$UTILS_LAYER_VERSION_ARN" \
            --timeout 300 \
            --memory-size 512 \
            --region "$AWS_REGION" \
            --query 'FunctionArn' \
            --output text > /dev/null

        FUNCTION_ARN=$(aws lambda get-function --function-name "$LAMBDA_FUNCTION_NAME" --region "$AWS_REGION" --query 'Configuration.FunctionArn' --output text)
    else
        log_info "Creating new Lambda function..."

        FUNCTION_ARN=$(aws lambda create-function \
            --function-name "$LAMBDA_FUNCTION_NAME" \
            --runtime python3.12 \
            --role "$ROLE_ARN" \
            --handler "lambda_main.lambda_handler" \
            --zip-file "fileb://${LAMBDA_ZIP}" \
            --layers "$LAYER_VERSION_ARN" "$UTILS_LAYER_VERSION_ARN" \
            --timeout 300 \
            --memory-size 512 \
            --region "$AWS_REGION" \
            --query 'FunctionArn' \
            --output text)

        # Wait for function to be active
        aws lambda wait function-active --function-name "$LAMBDA_FUNCTION_NAME" --region "$AWS_REGION"
    fi

    log_info "Lambda function deployed: ${FUNCTION_ARN}"
}

configure_function_url() {
    log_info "Configuring Lambda Function URL..."

    # Check if function URL exists
    EXISTING_URL=$(aws lambda get-function-url-config \
        --function-name "$LAMBDA_FUNCTION_NAME" \
        --region "$AWS_REGION" \
        --query 'FunctionUrl' \
        --output text 2>/dev/null || echo "")

    if [[ -n "$EXISTING_URL" && "$EXISTING_URL" != "None" ]]; then
        log_info "Function URL already exists: ${EXISTING_URL}"
        FUNCTION_URL="$EXISTING_URL"
    else
        # Create function URL with CORS
        FUNCTION_URL=$(aws lambda create-function-url-config \
            --function-name "$LAMBDA_FUNCTION_NAME" \
            --auth-type NONE \
            --cors 'AllowOrigins=*,AllowMethods=*,AllowHeaders=*,MaxAge=86400' \
            --region "$AWS_REGION" \
            --query 'FunctionUrl' \
            --output text)

        # Add permission for public access
        aws lambda add-permission \
            --function-name "$LAMBDA_FUNCTION_NAME" \
            --statement-id "FunctionURLAllowPublicAccess" \
            --action "lambda:InvokeFunctionUrl" \
            --principal "*" \
            --function-url-auth-type NONE \
            --region "$AWS_REGION" 2>/dev/null || true

        log_info "Function URL created: ${FUNCTION_URL}"
    fi
}

update_frontend() {
    log_info "Updating frontend with Lambda endpoint..."

    JS_FILE="${SCRIPT_DIR}/app.js"

    if [[ -f "$JS_FILE" ]]; then
        if [[ "$OSTYPE" == "darwin"* ]]; then
            sed -i '' "s|const LAMBDA_ENDPOINT = .*|const LAMBDA_ENDPOINT = '${FUNCTION_URL}'|" "$JS_FILE"
        else
            sed -i "s|const LAMBDA_ENDPOINT = .*|const LAMBDA_ENDPOINT = '${FUNCTION_URL}'|" "$JS_FILE"
        fi
        log_info "Updated app.js with Lambda endpoint."
    else
        log_warn "app.js not found. Manually set LAMBDA_ENDPOINT to: ${FUNCTION_URL}"
    fi
}

# ============================================================================
# Cleanup
# ============================================================================

cleanup() {
    log_info "Cleaning up build artifacts..."
    rm -rf "${BUILD_DIR}"
    log_info "Cleanup complete."
}

# ============================================================================
# Main
# ============================================================================

print_usage() {
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Options:"
    echo "  --skip-iam          Skip IAM role creation (requires --role-arn)"
    echo "  --role-arn <arn>    Use existing IAM role ARN"
    echo "  --update-code       Only update Lambda code (skip layer and IAM)"
    echo "  --skip-cleanup      Keep build artifacts after deployment"
    echo "  --help              Show this help message"
    echo ""
    echo "Before running, edit this script to set:"
    echo "  S3_BUCKET_NAME      Your S3 bucket name"
    echo "  AWS_REGION          Your AWS region"
}

main() {
    SKIP_IAM=false
    UPDATE_CODE_ONLY=false
    SKIP_CLEANUP=false
    ROLE_ARN=""

    # Parse arguments
    while [[ $# -gt 0 ]]; do
        case $1 in
            --skip-iam)
                SKIP_IAM=true
                shift
                ;;
            --role-arn)
                ROLE_ARN="$2"
                SKIP_IAM=true
                shift 2
                ;;
            --update-code)
                UPDATE_CODE_ONLY=true
                shift
                ;;
            --skip-cleanup)
                SKIP_CLEANUP=true
                shift
                ;;
            --help)
                print_usage
                exit 0
                ;;
            *)
                log_error "Unknown option: $1"
                print_usage
                exit 1
                ;;
        esac
    done

    echo "=============================================="
    echo "  Academic Paper Reviewer - AWS Deployment"
    echo "=============================================="
    echo ""

    check_prerequisites
    validate_config

    # Get AWS account info
    AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query 'Account' --output text)
    log_info "Deploying to AWS Account: ${AWS_ACCOUNT_ID}, Region: ${AWS_REGION}"

    if [[ "$UPDATE_CODE_ONLY" == true ]]; then
        log_info "Update code only mode - skipping IAM and layer..."

        # Get existing pymupdf layer version
        LAYER_VERSION_ARN=$(aws lambda list-layer-versions \
            --layer-name "$LAMBDA_LAYER_NAME" \
            --region "$AWS_REGION" \
            --query 'LayerVersions[0].LayerVersionArn' \
            --output text)

        if [[ -z "$LAYER_VERSION_ARN" || "$LAYER_VERSION_ARN" == "None" ]]; then
            log_error "No existing pymupdf layer found. Run full deployment first."
            exit 1
        fi

        # Get existing utilities layer version
        UTILS_LAYER_NAME="${LAMBDA_LAYER_NAME}-utils"
        UTILS_LAYER_VERSION_ARN=$(aws lambda list-layer-versions \
            --layer-name "$UTILS_LAYER_NAME" \
            --region "$AWS_REGION" \
            --query 'LayerVersions[0].LayerVersionArn' \
            --output text)

        if [[ -z "$UTILS_LAYER_VERSION_ARN" || "$UTILS_LAYER_VERSION_ARN" == "None" ]]; then
            log_error "No existing utilities layer found. Run full deployment first."
            exit 1
        fi

        package_lambda
        deploy_lambda_function
    else
        # Full deployment
        if [[ "$SKIP_IAM" == false ]]; then
            create_iam_role
        else
            if [[ -z "$ROLE_ARN" ]]; then
                # Try to get existing role
                ROLE_ARN=$(aws iam get-role --role-name "$IAM_ROLE_NAME" --query 'Role.Arn' --output text 2>/dev/null || echo "")
                if [[ -z "$ROLE_ARN" ]]; then
                    log_error "No role ARN provided and default role not found. Use --role-arn or remove --skip-iam"
                    exit 1
                fi
            fi
            log_info "Using existing IAM role: ${ROLE_ARN}"
        fi

        build_lambda_layer
        build_utilities_layer
        deploy_lambda_layer
        deploy_utilities_layer
        package_lambda
        deploy_lambda_function
        configure_function_url
        update_frontend
    fi

    if [[ "$SKIP_CLEANUP" == false ]]; then
        cleanup
    fi

    echo ""
    echo "=============================================="
    echo "  Deployment Complete!"
    echo "=============================================="
    echo ""
    echo "Lambda Function: ${LAMBDA_FUNCTION_NAME}"
    echo "Function URL:    ${FUNCTION_URL:-'(use --update-code for existing deployment)'}"
    echo "Region:          ${AWS_REGION}"
    echo "S3 Bucket:       ${S3_BUCKET_NAME}"
    echo ""
    if [[ -n "$FUNCTION_URL" ]]; then
        echo "Next steps:"
        echo "  1. Open index.html in your browser"
        echo "  2. Enter your AWS credentials"
        echo "  3. Upload a PDF and analyze!"
        echo ""
    fi
}

main "$@"
