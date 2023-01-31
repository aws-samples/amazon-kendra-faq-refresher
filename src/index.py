# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of this
# software and associated documentation files (the "Software"), to deal in the Software
# without restriction, including without limitation the rights to use, copy, modify,
# merge, publish, distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
# INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A
# PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
# HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
# SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

import boto3
import os
from datetime import datetime
from botocore.config import Config
import time

from aws_lambda_powertools import Tracer  # type: ignore
from aws_lambda_powertools import Logger  # type: ignore

tracer = Tracer(service='kendra-faq-refresher-lambda')
logger = Logger(service='kendra-faq-refresher-lambda')

FAQ_BUCKET = os.environ.get('FAQ_BUCKET')
KENDRA_FAQ_ROLE = os.environ.get('KENDRA_FAQ_ROLE')
SNS_TOPIC_ARN = os.environ['SNS_TOPIC_ARN']

config = Config(
    retries=dict(
        max_attempts=10
    )
)
client = boto3.client('kendra', config=config)
sns_client = boto3.client('sns')

@tracer.capture_method
def lambda_handler(event, context):
    """
    Lambda Function handler
    event: Lambda Event
    context: Lambda Context
    """
    description = None
    file_format = None
    faq_list_response = None
    faq_summary_list = list()

    for object_name in event.get('Records'):
        try:
            # S3 object_key replaces spaces with +, hence revert back
            object_key = object_name.get('s3').get('object').get('key').replace('+', ' ')
            index_id = object_key.split('/')[0].split('faq-')[1]
            document_name = object_key.split('/')[-1]
            # json or csv FAQ type files are named as <faq_name_prefix>-desc-<description>.<json|csv>
            # csv with header FAQ type files are named as header_<faq_name_prefix>-desc-<description>.csv
            if '-desc-' in document_name:
                description = document_name.split('-desc-')[1].split('.')[0]
                document_name = document_name.split('-desc-')[0]
                document_extension = object_key.split('.')[-1]
            else:
                document_name = object_key.split('/')[-1].split('.')[0]
                document_extension = object_key.split('.')[-1]
            document_extension = object_key.split('.')[-1]
            try:
                faq_list_response = client.list_faqs(
                    IndexId=index_id,
                )
                for faq_summaries in faq_list_response.get("FaqSummaryItems"):
                    faq_summary_list.append(faq_summaries)
                while faq_list_response.get("NextToken"):
                    faq_list_response = client.list_faqs(
                        IndexId=index_id,
                        NextToken=faq_list_response.get("NextToken")
                    )
                    for faq_summaries in faq_list_response.get("FaqSummaryItems"):
                        faq_summary_list.append(faq_summaries)
                logger.info(faq_summary_list)
            except Exception as error:
                logger.error(error)
                raise RuntimeError(f"Failed to list FAQ for {index_id} Index")

            try:
                file_format = document_extension.upper()
                if 'header_' in document_name:
                    file_format = 'CSV_WITH_HEADER'
            except:
                raise RuntimeError('Failed to format document extension')

            if file_format is not None and file_format in {'JSON', 'CSV', 'CSV_WITH_HEADER'}:
                now = datetime.now()
                faq_name = f"{document_name}-{document_extension}-faq-{now.strftime('%d-%m-%Y-%H-%M-%S')}"

                # Create New FAQ
                try:
                    response = client.create_faq(
                        IndexId=index_id,
                        Name=faq_name,
                        Description=description if description is not None else document_name,
                        S3Path={
                            'Bucket': FAQ_BUCKET,
                            'Key': object_key
                        },
                        RoleArn=KENDRA_FAQ_ROLE,
                        FileFormat=file_format,
                    )
                    logger.info(f'Successfully created FAQ: {faq_name}')
                except Exception as error:
                    logger.error(error)
                    sns_client.publish(
                        TargetArn = SNS_TOPIC_ARN,
                        Subject = f'[Error] FAQ upload failed',
                        Message = f"Kendra Index ID: {index_id}\n\nS3 bucket: {FAQ_BUCKET}\n\nFile: {object_key}\n\nSomething went wrong when uploading a new Kendra FAQ. Please investigate the Lambda Function's CloudWatch Log Group for more information.\n\nThank you."
                    )
                    logger.info('Publish to SNS Topic for FAQ failed uploads')
                    raise RuntimeError(f'Failed to create FAQ for Kendra Index: {index_id}')

                while True:
                    # Get the details of the FAQ, such as the status
                    describe_response = client.describe_faq(
                        Id=response.get('Id'),
                        IndexId=index_id
                    )
                    # When status is Active quit.
                    status = describe_response['Status']
                    logger.info(f"FAQ {response.get('Id')} status is {status}...")
                    if status == 'ACTIVE':
                        break
                    else:
                        time.sleep(5)

                # Delete Old FAQ
                for faq in faq_summary_list:
                    if f"{document_name}-{document_extension}-faq" in faq.get('Name'):
                        try:
                            client.delete_faq(
                                Id=faq.get('Id'),
                                IndexId=index_id
                            )
                            logger.info(f"Successfully deleted FAQ ID {faq.get('Id')}")
                        except Exception as error:
                            sns_client.publish(
                                TargetArn = SNS_TOPIC_ARN,
                                Subject = f'[Error] FAQ delete failed',
                                Message = f"Kendra Index ID: {index_id}\n\nFAQ ID: {faq.get('Id')}\n\nKendra FAQ failed to be deleted. Please investigate the Lambda Function's CloudWatch Log Group for more information.\n\nThank you."
                            )
                            logger.info('Publish to SNS Topic for FAQ failed deletes')
                            logger.error(f"Failed to delete FAQ {faq.get('Id')} for {index_id} Index")
                            logger.error(error)
                sns_client.publish(
                TargetArn = SNS_TOPIC_ARN,
                Subject = f"New FAQ {response.get('Id')} has been successfully added",
                Message = f"Kendra Index ID: {index_id}\n\nFAQ ID: {response.get('Id')}\n\nNew Kendra FAQ has been successfully added by the FAQ automation solution. Older versions (if any) have been deleted. You may now upload new versions if required.\n\nThank you."
                )
                logger.info('Publish to SNS Topic for FAQ Successful upload and old FAQ deletes')
            else:
                sns_client.publish(
                    TargetArn = SNS_TOPIC_ARN,
                    Subject = f'[Error] FAQ invalid file format',
                    Message = f"An invalid file format was uploaded to S3 for the Kendra FAQ automation solution to process. Please ensure that only 'JSON', 'CSV', or 'CSV_WITH_HEADER' file formats are uploaded.\n\nThank you."
                )
                logger.info('Publish to SNS Topic for invalid FAQ file formats')
                logger.error(f'Invalid File Format found for FAQ Document: {document_name}')
        except Exception as error:
            sns_client.publish(
                TargetArn = SNS_TOPIC_ARN,
                Subject = f'[Error] S3 processing',
                Message = f"Error encountered when reading the S3 event. Please investigate the Lambda Function's CloudWatch Log Group for more information.\n\nThank you."
            )
            logger.info('Publish to SNS Topic for S3 event processing failures')
            logger.error(f'Error processing S3 event {object}')
            logger.error(error)