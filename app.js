// ============================================
// CONFIGURATION - UPDATE THIS AFTER DEPLOYMENT
// ============================================
// After deploying the Lambda function, replace the URL below with your Function URL
// Example: 'https://abc123xyz.lambda-url.us-east-1.on.aws/'
const LAMBDA_ENDPOINT = ''


document.addEventListener('DOMContentLoaded', function () {
    const form = document.getElementById('reviewForm');
    const submitBtn = document.getElementById('submitBtn');
    const loading = document.getElementById('loading');
    const resultsTextarea = document.getElementById('results');
    const fileInput = document.getElementById('fileInput');
    const promptInput = document.getElementById('prompt');
    const referenceInput = document.getElementById('referenceInput')
    const fileError = document.getElementById('fileError');
    const promptError = document.getElementById('promptError');
    const referencesLLMCheck = document.getElementById('referencesLLMCheck');
    const referencesCheckResults = document.getElementById('referencesCheckResults');

    // Enable/disable checkbox based on referenceInput content
    referenceInput.addEventListener('input', function () {
        const hasText = referenceInput.value.trim().length > 0;
        referencesLLMCheck.disabled = !hasText;
        if (!hasText) {
            referencesLLMCheck.checked = false;
        }
    });

    form.addEventListener('submit', async function (e) {
        e.preventDefault();

        // Clear previous errors and results
        fileError.classList.remove('active');
        promptError.classList.remove('active');
        resultsTextarea.value = '';
        referencesCheckResults.value = '';

        // Validate form
        if (!validateForm()) {
            return;
        }

        // Get form values
        const accessKeyId = document.getElementById('accessKeyId').value.trim();
        const secretAccessKey = document.getElementById('secretAccessKey').value.trim();
        const sessionToken = document.getElementById('sessionToken').value.trim();
        const prompt = promptInput.value.trim();
        const file = fileInput.files[0];
        const referencesTitle = referenceInput.value.trim();

        if (!file) {
            showError(fileError, 'Please select a pdf file');
            return;
        }

        try {
            // Show loading state
            setLoadingState(true);

            // Read file content
            const fileBase64 = await readFileContent(file)

            // Old text-only read
            //const fileContent = await readFileContent(file);

            // Construct request payload
            const payload = {
                credentials: {  // This is not safe and only meant to be used by internal team
                    accessKeyId: accessKeyId,
                    secretAccessKey: secretAccessKey
                },
                fileBase64: fileBase64,
                prompt: prompt,
                referencesTitle: referencesTitle,
                runReferencesCheck: referencesLLMCheck.checked,
                fileName: file.name,
                contentType: file.type || "application/pdf",
            };

            // Add session token if provided
            if (sessionToken) {
                payload.credentials.sessionToken = sessionToken;
            }

            // Send request to Lambda
            const response = await sendRequest(payload);

            // Display results
            if (response.success) {
                resultsTextarea.value = response.result;

                // Display references validation results
                if (response.referencesValidation !== undefined) {
                    let validationText = '';
                    const hasMismatches = response.referencesValidation.length > 0;
                    const hasLookupErrors = response.referencesLookupErrors && response.referencesLookupErrors.length > 0;

                    if (!hasMismatches && !hasLookupErrors) {
                        validationText = 'All references validated.';
                    } else {
                        if (hasMismatches) {
                            validationText += 'Failed validations on references:\n\n';
                            response.referencesValidation.forEach((pair, index) => {
                                validationText += `Mismatch ${index + 1}:\n`;
                                validationText += `  Extracted: ${JSON.stringify(pair[0])}\n`;
                                validationText += `  Scholar: ${JSON.stringify(pair[1])}\n\n`;
                            });
                        }

                        if (hasLookupErrors) {
                            validationText += 'References that could not be looked up (API errors/not found):\n\n';
                            response.referencesLookupErrors.forEach((title, index) => {
                                validationText += `${index + 1}. ${title}\n\n`;
                            });
                        }
                    }
                    referencesCheckResults.value = validationText;
                }
            } else {
                resultsTextarea.value = 'Error: ' + (response.error || 'Unknown error occurred');
            }

        } catch (error) {
            // Handle errors
            resultsTextarea.value = 'Error: ' + error.message;
        } finally {
            // Hide loading state
            setLoadingState(false);
        }
    });

    function validateForm() {
        let isValid = true;

        // Validate prompt
        const prompt = promptInput.value.trim();
        if (!prompt) {
            showError(promptError, 'Please enter a prompt');
            isValid = false;
        }

        // Validate file
        if (!fileInput.files || fileInput.files.length === 0) {
            showError(fileError, 'Please select a text file');
            isValid = false;
        }

        return isValid;
    }

    function readFileContent(file) {
        return new Promise((resolve, reject) => {
            const reader = new FileReader();

            reader.onload = (e) => {
                // e.g., "data:application/pdf;base64,JVBERi0xLjcKJc..."
                const dataUrl = e.target.result;
                const base64 = dataUrl.split(',')[1]; // strip "data:*/*;base64,"
                resolve(base64);
            };

            reader.onerror = (e) => {
                reject(new Error('Failed to read file: ' + e.target.error));
            };

            reader.readAsDataURL(file);
        });
    }

    // Old function that accepted text only
    // function readFileContent(file) {
    //     return new Promise((resolve, reject) => {
    //         const reader = new FileReader();

    //         reader.onload = function (e) {
    //             resolve(e.target.result);
    //         };

    //         reader.onerror = function (e) {
    //             reject(new Error('Failed to read file: ' + e.target.error));
    //         };

    //         reader.readAsText(file);
    //     });
    // }

    async function sendRequest(payload) {
        try {
            const response = await fetch(LAMBDA_ENDPOINT, {
                method: 'POST',
                headers: {
                    'Content-Type': "application/json"
                },
                body: JSON.stringify(payload)
            });

            if (!response.ok) {
                throw new Error('Failed to connect to server. Status: ' + response.status);
            }

            const data = await response.json();
            return data;

        } catch (error) {
            if (error.name === 'TypeError' && error.message.includes('fetch')) {
                throw new Error('Failed to connect to server. Please check your connection');
            }
            throw error;
        }
    }

    function setLoadingState(isLoading) {
        if (isLoading) {
            submitBtn.disabled = true;
            loading.classList.add('active');
        } else {
            submitBtn.disabled = false;
            loading.classList.remove('active');
        }
    }

    function showError(errorElement, message) {
        errorElement.textContent = message;
        errorElement.classList.add('active');
    }
});
